"""처리 이력 원장(ledger) — SQLite 단일 파일. autopilot 상태 머신의 단일 진실 소스.

어떤 video_id 가 발견/스코어/선택/처리/업로드 어느 단계인지 기록해 중복 처리를 막고
재시도·감사 추적을 가능하게 한다. stdlib sqlite3 (WAL) — 의존성 없음.

상태 흐름(Phase 1 은 scored 까지만 자동, selected 는 사람이 mark):
  discovered → scored → selected → processing → qa_passed → pending_approval
  → approved → uploaded   (+ 어디서든 failed / skipped)
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from typing import Any, Optional  # noqa: E402

from engine.common import ensure_dir, get_logger, resolve_path  # noqa: E402

log = get_logger("ledger")

STATES = ("discovered", "scored", "selected", "processing", "qa_passed",
          "pending_approval", "approved", "uploaded", "failed", "skipped")

# 허용 전이 — 역행(uploaded→selected 등)을 막아 중복 처리·중복 업로드를 원장 차원에서 차단.
# 예외 상황은 set_state(force=True) 로만(감사 추적을 위해 notes 권장).
TRANSITIONS: dict[str, set[str]] = {
    "discovered": {"scored", "selected", "skipped", "failed"},
    "scored": {"selected", "skipped", "discovered", "failed"},   # →discovered = 재스코어
    "selected": {"processing", "skipped", "discovered", "failed"},
    "processing": {"qa_passed", "failed"},
    "qa_passed": {"pending_approval", "failed"},
    "pending_approval": {"approved", "skipped", "failed"},
    "approved": {"uploaded", "failed"},
    "uploaded": set(),                                           # 종착 — 되돌림은 force 만
    "failed": {"discovered", "selected", "processing"},          # 재시도 경로
    "skipped": {"discovered", "selected"},                       # 사람이 번복 가능
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id      TEXT PRIMARY KEY,
    title         TEXT,
    url           TEXT,
    duration      REAL,
    view_count    INTEGER,
    like_count    INTEGER,
    comment_count INTEGER,
    published_at  TEXT,
    state         TEXT NOT NULL DEFAULT 'discovered',
    level_guess   TEXT,
    score         REAL,
    scores        TEXT,             -- 신호별 세부 점수 JSON
    notes         TEXT,
    discovered_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_videos_state ON videos(state);
CREATE TABLE IF NOT EXISTS kpi_snapshots (           -- 자가개선: 게시 영상 성과 추적(2026-07-21)
    video_id    TEXT NOT NULL,                       -- 원장 video_id (원본 기준)
    youtube_id  TEXT NOT NULL,                       -- 게시된 JP 영상 id
    taken_at    TEXT NOT NULL,                       -- UTC ISO
    views       INTEGER,
    likes       INTEGER,
    comments    INTEGER,
    PRIMARY KEY (video_id, taken_at)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Postgres 백엔드 (B안 2단계 ②, 2026-08-14) ────────────────────────────
# 원장을 Supabase(중앙)로 옮기기 위한 이중 백엔드. 기본은 sqlite — 전환은
# config ledger.backend=postgres (또는 env LOOPY_LEDGER_BACKEND) 하나로 한다.
# 스키마·쿼리는 그대로 두고 연결만 갈아끼운다: search_path=loopy 라 raw SQL 의
# "videos"/"kpi_snapshots" 가 loopy.* 를 가리키고, '?' 는 쉼이 '%s' 로 바꾼다.
def pick_backend(config, env: Optional[dict] = None) -> str:
    """원장 백엔드 결정 — config ledger.backend > env > 기본 sqlite. 순수 — 테스트 대상."""
    import os
    env = os.environ if env is None else env
    v = str(((config or {}).get("ledger") or {}).get("backend")
            or env.get("LOOPY_LEDGER_BACKEND") or "sqlite").lower()
    if v not in ("sqlite", "postgres"):
        raise ValueError(f"ledger.backend 는 sqlite|postgres (받은 값: {v})")
    return v


def q_pg(sql: str) -> str:
    """sqlite 플레이스홀더 → psycopg. 원장 SQL 은 문자열 리터럴에 ? 를 안 쓴다(전수 확인
    2026-08-14) — 단순 치환으로 충분. 순수 — 테스트 대상."""
    return sql.replace("?", "%s")


class _PgConn:
    """sqlite3.Connection 의 원장 사용 부분집합 흉내 — execute/commit/close.
    행은 dict(RealDictCursor) — sqlite3.Row 처럼 r["k"]·dict(r) 이 된다."""
    is_pg = True

    def __init__(self, pg):
        self._pg = pg

    def execute(self, sql, params=()):
        import psycopg2.extras
        cur = self._pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(q_pg(sql), tuple(params))
        return cur

    def commit(self):
        self._pg.commit()

    def close(self):
        self._pg.close()


def _pg_connect() -> "_PgConn":
    import os
    try:
        import psycopg2  # noqa: F401
    except ImportError as e:
        raise ImportError("psycopg2-binary 필요: pip install psycopg2-binary") from e
    import psycopg2
    url = os.environ.get("PIPELINE_DB_URL")
    if not url:
        raise RuntimeError("PIPELINE_DB_URL 없음 — postgres 원장은 워커(ves 에이전트) env "
                           "아래에서만 쓸 수 있다(secrets/ves.env)")
    conn = _PgConn(psycopg2.connect(url, options="-c search_path=loopy,public"))
    row = conn.execute("SELECT to_regclass('loopy.videos') AS t").fetchone()
    if not row or not row.get("t"):
        raise RuntimeError("loopy.videos 없음 — ves 마이그레이션 0036 적용 후 사용")
    return conn


def connect(db_path: Optional[str] = None, config: Optional[dict[str, Any]] = None) -> sqlite3.Connection:
    """원장 DB 연결(없으면 생성). WAL 모드 — 프로세스 크래시에도 상태 보존."""
    if db_path is None and pick_backend(config) == "postgres":
        return _pg_connect()                      # 중앙 원장(0036) — 아무 맥에서나
    if db_path is None:
        apcfg = (config or {}).get("autopilot", {})
        db_path = str(resolve_path(apcfg.get("ledger_path", "outputs/autopilot.db")))
    ensure_dir(pathlib.Path(db_path).parent)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    # 마이그레이션: 기존 DB 에 없는 컬럼 추가(멱등)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
    if "publish_at" not in cols:                    # 예약 공개 슬롯(하루 1편 페이스 추적)
        conn.execute("ALTER TABLE videos ADD COLUMN publish_at TEXT")
    if "youtube_id" not in cols:                    # 업로드된 유튜브 영상 id
        conn.execute("ALTER TABLE videos ADD COLUMN youtube_id TEXT")
    return conn


def upsert_discovered(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    """스카우트 결과 반영. 신규는 discovered 로 삽입, 기존은 지표만 갱신(상태 불변).

    반환: 신규 삽입 수.
    """
    new = 0
    now = _now()
    for r in rows:
        vid = r.get("video_id")
        if not vid:
            continue
        exists = conn.execute("SELECT 1 FROM videos WHERE video_id=?", (vid,)).fetchone()
        if exists:
            conn.execute(
                """UPDATE videos SET
                     title=COALESCE(?, title), duration=COALESCE(?, duration),
                     view_count=COALESCE(?, view_count), like_count=COALESCE(?, like_count),
                     comment_count=COALESCE(?, comment_count),
                     published_at=COALESCE(?, published_at), updated_at=?
                   WHERE video_id=?""",
                (r.get("title"), r.get("duration"), r.get("view_count"), r.get("like_count"),
                 r.get("comment_count"), r.get("published_at"), now, vid))
        else:
            conn.execute(
                """INSERT INTO videos (video_id, title, url, duration, view_count, like_count,
                                       comment_count, published_at, state, discovered_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?, 'discovered', ?, ?)""",
                (vid, r.get("title"), r.get("url"), r.get("duration"), r.get("view_count"),
                 r.get("like_count"), r.get("comment_count"), r.get("published_at"), now, now))
            new += 1
    conn.commit()
    return new


def get_by_state(conn: sqlite3.Connection, state: str,
                 limit: Optional[int] = None) -> list[dict[str, Any]]:
    if state not in STATES:
        raise ValueError(f"알 수 없는 상태: {state} ({'/'.join(STATES)})")
    q = "SELECT * FROM videos WHERE state=? ORDER BY view_count DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(q, (state,)).fetchall()]


def set_state(conn: sqlite3.Connection, video_id: str, state: str,
              notes: Optional[str] = None, force: bool = False) -> None:
    if state not in STATES:
        raise ValueError(f"알 수 없는 상태: {state} ({'/'.join(STATES)})")
    row = conn.execute("SELECT state FROM videos WHERE video_id=?", (video_id,)).fetchone()
    if row is None:
        raise KeyError(f"원장에 없는 video_id: {video_id}")
    current = row["state"]
    if not force and state != current and state not in TRANSITIONS.get(current, set()):
        raise ValueError(
            f"허용되지 않는 전이: {current} → {state} (video {video_id}). "
            f"허용: {sorted(TRANSITIONS.get(current, set())) or '없음(종착)'} — 예외는 force=True")
    conn.execute(
        "UPDATE videos SET state=?, notes=COALESCE(?, notes), updated_at=? WHERE video_id=?",
        (state, notes, _now(), video_id))
    conn.commit()


def record_score(conn: sqlite3.Connection, video_id: str, total: float,
                 scores: dict[str, Any], level_guess: Optional[str] = None) -> None:
    """스코어링 결과 기록 + 상태 scored 전이 (discovered/scored 에서만 가능)."""
    cur = conn.execute(
        """UPDATE videos SET score=?, scores=?, level_guess=?, state='scored', updated_at=?
           WHERE video_id=? AND state IN ('discovered','scored')""",
        (round(float(total), 6), json.dumps(scores, ensure_ascii=False),
         level_guess, _now(), video_id))
    if cur.rowcount == 0:
        row = conn.execute("SELECT state FROM videos WHERE video_id=?", (video_id,)).fetchone()
        if row is None:
            raise KeyError(f"원장에 없는 video_id: {video_id}")
        raise ValueError(f"스코어 기록 불가: {video_id} 는 {row['state']} 상태(처리 진행 중 보호)")
    conn.commit()


def record_upload(conn: sqlite3.Connection, video_id: str, youtube_id: str,
                  publish_at: str) -> None:
    """업로드 성공 기록 + uploaded 전이(approved 에서만 — 전이 규칙이 검증)."""
    set_state(conn, video_id, "uploaded",
              notes=f"https://youtu.be/{youtube_id} publishAt={publish_at}")
    conn.execute("UPDATE videos SET youtube_id=?, publish_at=?, updated_at=? WHERE video_id=?",
                 (youtube_id, publish_at, _now(), video_id))
    conn.commit()


def taken_publish_slots(conn: sqlite3.Connection) -> set[str]:
    """이미 잡힌 예약 슬롯(하루 1편 페이스의 근거)."""
    return {r["publish_at"] for r in conn.execute(
        "SELECT publish_at FROM videos WHERE publish_at IS NOT NULL")}


def top_scored(conn: sqlite3.Connection, n: int = 10) -> list[dict[str, Any]]:
    """리포트용 — scored 상태 상위 n편(점수순)."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM videos WHERE state='scored' ORDER BY score DESC LIMIT ?", (n,))]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["state"]: r["n"] for r in conn.execute(
        "SELECT state, COUNT(*) AS n FROM videos GROUP BY state")}


# ── KPI 스냅샷 (자가개선: 게시 성과 추적, 2026-07-21) ─────────────────────
def record_kpi(conn: sqlite3.Connection, video_id: str, youtube_id: str,
               stats: dict[str, Any]) -> None:
    """일일 성과 스냅샷 기록(같은 시각 중복은 무시)."""
    conn.execute(kpi_insert_sql(getattr(conn, "is_pg", False)),
        (video_id, youtube_id, _now(), stats.get("views"), stats.get("likes"),
         stats.get("comments")))
    conn.commit()


def kpi_insert_sql(is_pg: bool) -> str:
    """같은 시각 중복 무시 upsert — 방언만 다르다. 순수 — 테스트 대상."""
    if is_pg:
        return ("INSERT INTO kpi_snapshots (video_id, youtube_id, taken_at, views, likes, "
                "comments) VALUES (?,?,?,?,?,?) ON CONFLICT (video_id, taken_at) DO NOTHING")
    return ("INSERT OR IGNORE INTO kpi_snapshots (video_id, youtube_id, taken_at, views, "
            "likes, comments) VALUES (?,?,?,?,?,?)")


def kpi_history(conn: sqlite3.Connection, video_id: str) -> list[dict[str, Any]]:
    """영상별 스냅샷 시계열(오래된 순)."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM kpi_snapshots WHERE video_id=? ORDER BY taken_at", (video_id,))]


def migrate_to_pg(config: Optional[dict[str, Any]] = None) -> dict[str, int]:
    """sqlite 원장 전체 → loopy.* (컷오버 1회용). 멱등 — video_id/(video_id,taken_at) upsert."""
    src = connect(config={**(config or {}), "ledger": {"backend": "sqlite"}})
    dst = _pg_connect()
    vids = [dict(r) for r in src.execute("SELECT * FROM videos")]
    for r in vids:
        cols = ("video_id", "title", "url", "duration", "view_count", "like_count",
                "comment_count", "published_at", "state", "level_guess", "score",
                "scores", "notes", "discovered_at", "updated_at", "publish_at", "youtube_id")
        dst.execute(
            "INSERT INTO videos (" + ",".join(cols) + ") VALUES (" + ",".join("?" * len(cols))
            + ") ON CONFLICT (video_id) DO UPDATE SET "
            + ",".join(f"{c}=excluded.{c}" for c in cols if c != "video_id"),
            tuple(r.get(c) for c in cols))
    kpis = [dict(r) for r in src.execute("SELECT * FROM kpi_snapshots")]
    for r in kpis:
        dst.execute(
            "INSERT INTO kpi_snapshots (video_id, youtube_id, taken_at, views, likes, comments)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT (video_id, taken_at) DO NOTHING",
            (r.get("video_id"), r.get("youtube_id"), r.get("taken_at"),
             r.get("views"), r.get("likes"), r.get("comments")))
    dst.commit(); dst.close(); src.close()
    log.info("원장 이관: videos %d행 · kpi %d행 → loopy.*", len(vids), len(kpis))
    return {"videos": len(vids), "kpi": len(kpis)}


if __name__ == "__main__":
    import argparse
    from engine.common import load_config
    ap = argparse.ArgumentParser(description="원장 유틸")
    ap.add_argument("cmd", choices=["migrate-to-pg"])
    a = ap.parse_args()
    if a.cmd == "migrate-to-pg":
        print(migrate_to_pg(load_config()))
