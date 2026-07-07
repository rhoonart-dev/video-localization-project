"""src/ledger.py — 원장 상태 머신 (임시 SQLite, 네트워크 없음)."""
import tempfile
from pathlib import Path

from src import ledger


def _conn(tmp):
    return ledger.connect(str(Path(tmp) / "test.db"))


ROWS = [
    {"video_id": "v1", "title": "루피 쇼츠", "url": "https://www.youtube.com/shorts/v1",
     "duration": 30.0, "view_count": 1000, "like_count": 50, "comment_count": 5,
     "published_at": "2026-01-01T00:00:00Z"},
    {"video_id": "v2", "title": "루피 먹방", "url": "https://www.youtube.com/shorts/v2",
     "duration": 60.0, "view_count": 5000, "like_count": 100, "comment_count": 10,
     "published_at": "2026-02-01T00:00:00Z"},
]


def test_upsert_new_then_rescan_same_second_counts_zero():
    # 같은 초 안에 재스캔해도 기존 항목은 신규로 세지 않는다(중복 처리 방지의 핵심).
    with tempfile.TemporaryDirectory() as tmp:
        conn = _conn(tmp)
        assert ledger.upsert_discovered(conn, ROWS) == 2
        assert ledger.upsert_discovered(conn, ROWS) == 0


def test_upsert_updates_stats_but_not_state():
    with tempfile.TemporaryDirectory() as tmp:
        conn = _conn(tmp)
        ledger.upsert_discovered(conn, ROWS)
        ledger.set_state(conn, "v1", "scored")
        updated = [{**ROWS[0], "view_count": 9999}]
        assert ledger.upsert_discovered(conn, updated) == 0
        row = conn.execute("SELECT * FROM videos WHERE video_id='v1'").fetchone()
        assert row["view_count"] == 9999
        assert row["state"] == "scored"          # 상태는 스카우트가 건드리지 않는다


def test_upsert_skips_rows_without_id():
    with tempfile.TemporaryDirectory() as tmp:
        conn = _conn(tmp)
        assert ledger.upsert_discovered(conn, [{"title": "no id"}]) == 0


def test_set_state_validates():
    with tempfile.TemporaryDirectory() as tmp:
        conn = _conn(tmp)
        ledger.upsert_discovered(conn, ROWS)
        try:
            ledger.set_state(conn, "v1", "banana")
            assert False, "잘못된 상태를 거부해야 함"
        except ValueError:
            pass
        try:
            ledger.set_state(conn, "ghost", "scored")
            assert False, "없는 id 를 거부해야 함"
        except KeyError:
            pass


def test_record_score_and_top_scored_order():
    with tempfile.TemporaryDirectory() as tmp:
        conn = _conn(tmp)
        ledger.upsert_discovered(conn, ROWS)
        ledger.record_score(conn, "v1", 0.4, {"views": 0.1}, level_guess="A")
        ledger.record_score(conn, "v2", 0.9, {"views": 0.8}, level_guess="B")
        top = ledger.top_scored(conn, 10)
        assert [t["video_id"] for t in top] == ["v2", "v1"]
        assert top[0]["state"] == "scored" and top[0]["level_guess"] == "B"
        assert '"views"' in top[0]["scores"]      # 세부 점수 JSON 보존


def test_get_by_state_and_counts():
    with tempfile.TemporaryDirectory() as tmp:
        conn = _conn(tmp)
        ledger.upsert_discovered(conn, ROWS)
        ledger.set_state(conn, "v2", "skipped", notes="일본 부적합")
        assert [r["video_id"] for r in ledger.get_by_state(conn, "discovered")] == ["v1"]
        c = ledger.counts(conn)
        assert c == {"discovered": 1, "skipped": 1}
