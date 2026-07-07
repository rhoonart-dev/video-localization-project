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
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Optional[str] = None, config: Optional[dict[str, Any]] = None) -> sqlite3.Connection:
    """원장 DB 연결(없으면 생성). WAL 모드 — 프로세스 크래시에도 상태 보존."""
    if db_path is None:
        apcfg = (config or {}).get("autopilot", {})
        db_path = str(resolve_path(apcfg.get("ledger_path", "outputs/autopilot.db")))
    ensure_dir(pathlib.Path(db_path).parent)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
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


def top_scored(conn: sqlite3.Connection, n: int = 10) -> list[dict[str, Any]]:
    """리포트용 — scored 상태 상위 n편(점수순)."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM videos WHERE state='scored' ORDER BY score DESC LIMIT ?", (n,))]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["state"]: r["n"] for r in conn.execute(
        "SELECT state, COUNT(*) AS n FROM videos GROUP BY state")}
