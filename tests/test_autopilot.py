"""src/autopilot.py — 신호 조립·리포트 생성 순수 로직 (네트워크/LLM 없음)."""
from src.autopilot import build_report_md, build_signals, valid_level


def test_valid_level_rejects_llm_schema_violations():
    assert valid_level("A") == "A" and valid_level("C") == "C"
    assert valid_level("D") is None                # config.levels 에 없는 값
    assert valid_level(None) is None
    assert valid_level("자막") is None

CFG = {"autopilot": {"weights": {"views": 0.2, "like_ratio": 0.15,
                                 "jp_comments": 0.35, "llm_jp_fit": 0.3}}}


def test_build_signals_full():
    row = {"video_id": "v1", "view_count": 1000, "like_count": 50}
    llm = {"jp_fit": 8, "language_dependence": 2, "level_guess": "A", "reason": "비언어 유머"}
    sig = build_signals(row, jp_comment_ratio=0.4, llm_item=llm, max_views=1000)
    assert sig["views"] == 1.0
    assert sig["like_ratio"] == 1.0          # 5% 상한
    assert sig["jp_comments"] == 0.4
    assert sig["llm_jp_fit"] == 0.8          # (8 + (10-2)) / 20


def test_build_signals_missing_are_none():
    row = {"video_id": "v1", "view_count": None, "like_count": None}
    sig = build_signals(row, jp_comment_ratio=None, llm_item={}, max_views=1000)
    assert sig["jp_comments"] is None
    assert sig["llm_jp_fit"] is None
    assert sig["views"] == 0.0               # 조회수 미상 → 0 (보수적)


def test_build_report_md_contents_and_escape():
    rows = [{"video_id": "abc", "title": "루피|밈 쇼츠", "url": "https://www.youtube.com/shorts/abc",
             "view_count": 67000, "duration": 14.0, "score": 0.81, "level_guess": "A",
             "scores": '{"views": 0.9, "jp_comments": 0.5, "llm_reason": "사운드 중심"}'}]
    md = build_report_md(rows, generated_at="2026-07-07")
    assert "autopilot 후보 리포트" in md
    assert "| 1 |" in md                      # 순위
    assert "루피\\|밈 쇼츠" in md             # 표 파이프 이스케이프
    assert "shorts/abc" in md
    assert "0.81" in md
    assert "업로드하지 않는다" in md          # Phase 1 가드 문구
    assert "mark abc" in md                   # 다음 단계 안내


def test_build_report_md_empty():
    md = build_report_md([], generated_at="2026-07-07")
    assert "후보 없음" in md
