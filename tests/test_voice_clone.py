"""src/voice_clone.py — 레퍼런스 계획/설정/업로드 한도 순수 로직."""
from src.voice_clone import (UPLOAD_LIMIT_BYTES, plan_reference_segments,
                             recommended_voice_settings, upload_size_ok)


def _items(*segsets):
    return [{"path": f"/v{i}.wav", "segments": [{"start": s, "end": e} for s, e in segs]}
            for i, segs in enumerate(segsets)]


def test_plan_filters_short_segments():
    plan = plan_reference_segments(_items([(0.0, 0.5), (1.0, 3.0)]), min_dur=1.0)
    assert len(plan) == 1
    assert plan[0]["start"] == 1.0 and plan[0]["dur"] == 2.0


def test_plan_preserves_order_and_paths():
    plan = plan_reference_segments(_items([(0, 2)], [(5, 8)]))
    assert [p["path"] for p in plan] == ["/v0.wav", "/v1.wav"]


def test_plan_caps_total_duration():
    # 세그 10초 × 5개, 상한 25초 → 앞의 2개만 (3번째부터 초과)
    plan = plan_reference_segments(_items([(0, 10), (20, 30), (40, 50), (60, 70), (80, 90)]),
                                   max_total=25.0)
    assert len(plan) == 2


def test_plan_empty_input():
    assert plan_reference_segments([]) == []
    assert plan_reference_segments(_items([])) == []


def test_recommended_settings_are_mac6_pick():
    s = recommended_voice_settings()
    assert s["stability"] == 0.35
    assert s["similarity_boost"] == 1.0
    assert s["style"] == 0.6
    assert s["use_speaker_boost"] is True


def test_upload_size_boundary():
    assert upload_size_ok(UPLOAD_LIMIT_BYTES)
    assert not upload_size_ok(UPLOAD_LIMIT_BYTES + 1)
    assert not upload_size_ok(0)
