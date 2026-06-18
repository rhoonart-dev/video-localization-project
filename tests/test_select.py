"""src/select.py — 등급 추정 / 정규화 / 점수 / 랭킹 순수 로직."""
from src.select import composite_score, estimate_level, minmax, rank_rows, to_unit

KW = {"A": ["먹방", "asmr"], "B": ["개그"], "C": ["vlog"]}


def test_estimate_level_a():
    assert estimate_level("불닭 먹방 ASMR", KW) == "A"


def test_estimate_level_c():
    assert estimate_level("브이로그 vlog", KW) == "C"


def test_estimate_level_default_b():
    assert estimate_level("그냥 영상 제목", KW) == "B"


def test_to_unit_handles_percent_and_unit_and_bad():
    assert to_unit("48.5") == 0.485
    assert to_unit(0.32) == 0.32
    assert to_unit("bad") == 0.0


def test_minmax():
    assert minmax([10, 20, 30]) == [0.0, 0.5, 1.0]
    assert minmax([5, 5]) == [0.0, 0.0]


def test_composite_score_weights():
    w = {"jp_share": 0.5, "retention": 0.3, "views": 0.2}
    assert composite_score(1.0, 0.0, 0.0, w) == 0.5


def test_rank_rows_orders_by_jp_share_first():
    cfg = {"select": {
        "columns": {"video_id": "video_id", "title": "title", "views": "views",
                    "retention": "average_view_percentage", "jp_share": "jp_view_share"},
        "weights": {"jp_share": 0.5, "retention": 0.3, "views": 0.2},
        "level_keywords": KW}}
    rows = [
        {"video_id": "a", "title": "먹방", "views": "100",
         "average_view_percentage": "50", "jp_view_share": "0.1"},
        {"video_id": "b", "title": "먹방", "views": "100",
         "average_view_percentage": "50", "jp_view_share": "0.9"},
    ]
    ranked = rank_rows(rows, cfg)
    assert ranked[0]["video_id"] == "b" and ranked[0]["batch_order"] == 1
    assert ranked[0]["estimated_level"] == "A"
