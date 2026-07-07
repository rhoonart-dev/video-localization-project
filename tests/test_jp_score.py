"""src/jp_score.py — 일본 적합도 스코어링 순수 로직 (LLM 호출 없음)."""
from src.jp_score import (combine_scores, has_kana, kana_ratio, like_norm,
                          log_norm, parse_llm_array)


def test_has_kana_detects_japanese_not_korean_chinese():
    assert has_kana("ルーピーかわいい")            # 가타카나+히라가나
    assert has_kana("可愛いです")                  # 한자+히라가나
    assert not has_kana("루피 귀여워")             # 한국어
    assert not has_kana("可爱")                    # 중국어(한자만) — 가나 없음
    assert not has_kana("so cute!!")


def test_kana_ratio():
    texts = ["かわいい", "루피 짱", "ルーピー!", "nice"]
    assert kana_ratio(texts) == 0.5
    assert kana_ratio([]) is None                  # 표본 없음 → 신호 없음


def test_log_norm_monotonic_and_bounded():
    assert log_norm(0, 1000) == 0.0
    assert log_norm(1000, 1000) == 1.0
    mid = log_norm(100, 1000)
    assert 0.0 < mid < 1.0
    assert log_norm(10, 1000) < mid                # 단조 증가
    assert log_norm(5, 0) == 0.0                   # max 0 가드


def test_like_norm_caps_at_typical_good_ratio():
    assert like_norm(0, 1000) == 0.0
    assert like_norm(50, 1000) == 1.0              # 5% = 상한
    assert abs(like_norm(25, 1000) - 0.5) < 1e-9
    assert like_norm(10, 0) == 0.0                 # 조회수 0 가드


def test_combine_scores_renormalizes_missing_signals():
    w = {"views": 0.2, "like_ratio": 0.15, "jp_comments": 0.35, "llm_jp_fit": 0.3}
    # 모든 신호 존재
    full = combine_scores({"views": 1.0, "like_ratio": 1.0,
                           "jp_comments": 1.0, "llm_jp_fit": 1.0}, w)
    assert abs(full - 1.0) < 1e-9
    # jp_comments 신호 없음(None) → 남은 가중치로 재정규화(합=1 유지)
    part = combine_scores({"views": 1.0, "like_ratio": 1.0,
                           "jp_comments": None, "llm_jp_fit": 1.0}, w)
    assert abs(part - 1.0) < 1e-9
    # 신호가 전부 없으면 0
    assert combine_scores({"views": None, "like_ratio": None,
                           "jp_comments": None, "llm_jp_fit": None}, w) == 0.0


def test_parse_llm_array_with_fence_and_junk():
    fenced = '설명입니다.\n```json\n[{"video_id":"a","jp_fit":8}]\n```\n끝.'
    assert parse_llm_array(fenced) == [{"video_id": "a", "jp_fit": 8}]
    bare = '[{"video_id":"b","jp_fit":3}]'
    assert parse_llm_array(bare)[0]["video_id"] == "b"
    try:
        parse_llm_array("배열이 없어요")
        assert False, "JSON 배열 없으면 ValueError"
    except ValueError:
        pass


def test_llm_component_formula():
    # llm 세부점수 → 0~1 성분: (jp_fit + (10-언어의존)) / 20
    from src.jp_score import llm_component
    assert llm_component({"jp_fit": 10, "language_dependence": 0}) == 1.0
    assert llm_component({"jp_fit": 0, "language_dependence": 10}) == 0.0
    assert llm_component({"jp_fit": 5, "language_dependence": 5}) == 0.5
    assert llm_component({}) is None               # LLM 실패 → 신호 없음
