"""engine/qa_compare.py — 쌍 비교 QC 순수 로직."""
import pytest

from engine.qa_compare import (build_compare_prompt, content_top_from_motion,
                               pair_timestamps, parse_verdict, row_motion_counts,
                               verdict_pass)


def test_pair_timestamps_standard_clip():
    pts = pair_timestamps(20.0)
    assert pts == [1.0, 10.0, 18.5]


def test_pair_timestamps_short_clip_no_duplicates():
    pts = pair_timestamps(1.5)
    assert pts == sorted(set(pts))
    assert all(0 <= t <= 1.5 for t in pts)


def test_pair_timestamps_zero_duration():
    assert pair_timestamps(0) == [0.0]


def test_prompt_mentions_pair_semantics():
    system, user = build_compare_prompt(3)
    assert "JSON" in system
    assert "원본" in user and "소품" in user      # 쌍 비교·소품 예외가 명시돼야 오탐 방지
    assert "3세트" in user


def test_parse_verdict_plain_and_fenced():
    v = parse_verdict('{"korean_text_visible": false, "content_clipped": true, "notes": "x"}')
    assert v["content_clipped"] and not v["korean_text_visible"]
    v = parse_verdict('```json\n{"korean_text_visible": true, "content_clipped": false, '
                      '"notes": ""}\n```')
    assert v["korean_text_visible"]


def test_parse_verdict_garbage_raises():
    with pytest.raises(ValueError):
        parse_verdict("판정 불가")


def test_verdict_pass():
    assert verdict_pass({"korean_text_visible": False, "content_clipped": False, "notes": ""})
    assert not verdict_pass({"korean_text_visible": True, "content_clipped": False, "notes": ""})


def _synthetic_frames(w, h, content_top, n=5):
    """content_top 아래 행만 프레임마다 밝기가 변하는 합성 그레이스케일."""
    frames = []
    for i in range(n):
        f = []
        for row in range(h):
            val = 10 if row < content_top else (10 + i * 30)  # 위=정지, 아래=변화
            f.extend([val] * w)
        frames.append(f)
    return frames


def test_motion_finds_content_boundary():
    w, h, top = 20, 40, 25
    counts = row_motion_counts(_synthetic_frames(w, h, top), w, h, col_stride=1)
    assert content_top_from_motion(counts, min_hits=5) == top


def test_motion_none_when_all_static():
    w, h = 20, 40
    frames = [[10] * (w * h) for _ in range(5)]
    counts = row_motion_counts(frames, w, h)
    assert content_top_from_motion(counts) is None
