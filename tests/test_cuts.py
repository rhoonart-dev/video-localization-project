"""engine/cuts.py — E9 구간 잘라내기 순수 로직 (검증·당김·제외·여집합)."""
import pytest

from engine.cuts import (apply_cuts_to_events, cut_total, keep_segments,
                         shift_time, validate_cuts)
from engine.render import events_json_doc
from src.dub import build_dub_pairs, update_pairs_actual_ends


# ── validate_cuts ────────────────────────────────────────────────────────
def test_validate_none_and_empty():
    assert validate_cuts(None) == []
    assert validate_cuts([]) == []


def test_validate_sorts_and_normalizes():
    out = validate_cuts([{"start_sec": 80.2, "end_sec": 95.0},
                         {"start_sec": 34, "end_sec": 41.5}])
    assert out == [{"start_sec": 34.0, "end_sec": 41.5},
                   {"start_sec": 80.2, "end_sec": 95.0}]
    assert cut_total(out) == pytest.approx(7.5 + 14.8)


def test_validate_rejects_non_list_and_non_dict():
    with pytest.raises(ValueError):
        validate_cuts({"start_sec": 1, "end_sec": 2})
    with pytest.raises(ValueError):
        validate_cuts([[1, 2]])


def test_validate_rejects_bad_numbers():
    with pytest.raises(ValueError):
        validate_cuts([{"start_sec": "1", "end_sec": 2}])
    with pytest.raises(ValueError):
        validate_cuts([{"start_sec": True, "end_sec": 2}])   # bool 은 숫자 아님
    with pytest.raises(ValueError):
        validate_cuts([{"start_sec": 1}])                     # end_sec 누락


def test_validate_rejects_negative_start_and_bad_order():
    with pytest.raises(ValueError):
        validate_cuts([{"start_sec": -0.1, "end_sec": 2}])
    with pytest.raises(ValueError):
        validate_cuts([{"start_sec": 5, "end_sec": 5}])       # end > start 필요


def test_validate_rejects_overlap():
    with pytest.raises(ValueError):
        validate_cuts([{"start_sec": 1, "end_sec": 5},
                       {"start_sec": 4, "end_sec": 8}])
    # 맞닿음(end == 다음 start)은 허용
    assert len(validate_cuts([{"start_sec": 1, "end_sec": 5},
                              {"start_sec": 5, "end_sec": 8}])) == 2


def test_validate_rejects_over_max_count():
    cuts = [{"start_sec": i * 2.0, "end_sec": i * 2.0 + 1.0} for i in range(21)]
    with pytest.raises(ValueError):
        validate_cuts(cuts)


def test_validate_rejects_80pct_deletion():
    # 총 8s 삭제 / 원본 10s = 80% — 거절(실수 방지). duration 미상이면 통과.
    cuts = [{"start_sec": 0.0, "end_sec": 8.0}]
    with pytest.raises(ValueError):
        validate_cuts(cuts, duration=10.0)
    assert validate_cuts(cuts) == [{"start_sec": 0.0, "end_sec": 8.0}]
    assert len(validate_cuts([{"start_sec": 0.0, "end_sec": 7.9}], duration=10.0)) == 1


# ── shift_time ───────────────────────────────────────────────────────────
def test_shift_time_before_inside_after():
    cuts = validate_cuts([{"start_sec": 10.0, "end_sec": 15.0}])
    assert shift_time(3.0, cuts) == 3.0            # 컷 앞 — 그대로
    assert shift_time(12.0, cuts) == 10.0          # 컷 안 — 시작점으로 클램프
    assert shift_time(20.0, cuts) == 15.0          # 컷 뒤 — 5s 당김


def test_shift_time_multiple_cuts_accumulate():
    cuts = validate_cuts([{"start_sec": 10.0, "end_sec": 15.0},
                          {"start_sec": 30.0, "end_sec": 40.0}])
    assert shift_time(50.0, cuts) == 35.0          # 5 + 10 당김
    assert shift_time(35.0, cuts) == 25.0          # 둘째 컷 안 — 그 시작(30-5)으로


# ── apply_cuts_to_events ─────────────────────────────────────────────────
def _ev(idx, start, end, **kw):
    return {"idx": idx, "start": start, "end": end, "text": f"t{idx}", **kw}


def test_apply_no_cuts_returns_copies():
    events = [_ev(0, 1.0, 2.0)]
    out, n = apply_cuts_to_events(events, [])
    assert n == 0 and out == events and out[0] is not events[0]


def test_apply_drops_fully_inside_as_use_false():
    cuts = validate_cuts([{"start_sec": 10.0, "end_sec": 15.0}])
    out, n = apply_cuts_to_events([_ev(0, 11.0, 14.0)], cuts)
    assert n == 1 and out[0]["use"] is False       # use:false 와 동일 의미 — 표시만


def test_apply_clamps_straddling_to_boundary():
    cuts = validate_cuts([{"start_sec": 10.0, "end_sec": 15.0}])
    # 끝이 컷에 걸침 → end 가 경계(10.0)로
    out, _ = apply_cuts_to_events([_ev(0, 8.0, 12.0)], cuts)
    assert (out[0]["start"], out[0]["end"]) == (8.0, 10.0)
    # 시작이 컷에 걸침 → start 가 경계로, end 는 당김
    out, _ = apply_cuts_to_events([_ev(0, 13.0, 18.0)], cuts)
    assert (out[0]["start"], out[0]["end"]) == (10.0, 13.0)
    # 컷을 관통(양쪽에 살점) → 중간만 빠지고 이어 붙음
    out, _ = apply_cuts_to_events([_ev(0, 8.0, 18.0)], cuts)
    assert (out[0]["start"], out[0]["end"]) == (8.0, 13.0)


def test_apply_shifts_later_events_and_keeps_order():
    cuts = validate_cuts([{"start_sec": 10.0, "end_sec": 15.0}])
    events = [_ev(0, 1.0, 3.0), _ev(1, 20.0, 22.0, end_fixed=True)]
    out, n = apply_cuts_to_events(events, cuts)
    assert n == 0
    assert (out[0]["start"], out[0]["end"]) == (1.0, 3.0)
    # 사용자 지정 end(end_fixed)도 당김 대상 — 절대값이 아니라 그 장면에 붙어 있다
    assert (out[1]["start"], out[1]["end"]) == (15.0, 17.0)
    assert out[1]["end_fixed"] is True
    assert [e["start"] for e in out] == sorted(e["start"] for e in out)


def test_apply_existing_use_false_only_shifts():
    cuts = validate_cuts([{"start_sec": 1.0, "end_sec": 2.0}])
    out, n = apply_cuts_to_events([_ev(0, 5.0, 6.0, use=False)], cuts)
    assert n == 0                                   # 이미 빠질 줄 — 제외 수에 안 센다
    assert out[0]["use"] is False and out[0]["start"] == 4.0


# ── keep_segments (ffmpeg trim 입력) ─────────────────────────────────────
def test_keep_segments_complement():
    cuts = validate_cuts([{"start_sec": 10.0, "end_sec": 15.0},
                          {"start_sec": 30.0, "end_sec": 40.0}])
    assert keep_segments(cuts, 60.0) == [(0.0, 10.0), (15.0, 30.0), (40.0, None)]


def test_keep_segments_cut_at_zero_and_till_end():
    cuts = validate_cuts([{"start_sec": 0.0, "end_sec": 5.0}])
    assert keep_segments(cuts, 20.0) == [(5.0, None)]
    cuts = validate_cuts([{"start_sec": 15.0, "end_sec": 25.0}])   # 끝 넘는 컷 — 클램프
    assert keep_segments(cuts, 20.0) == [(0.0, 15.0)]


# ── C 루트 조합: cuts → pairs (당겨진 시각·제외·메타 유지) ────────────────
def test_pairs_after_cuts_shifted_and_dropped():
    cuts = validate_cuts([{"start_sec": 10.0, "end_sec": 15.0}])
    segs = [{"start": 1.0, "end": 3.0, "text": "ㄱ"},
            {"start": 11.0, "end": 14.0, "text": "ㄴ"},
            {"start": 20.0, "end": 22.0, "text": "ㄷ"}]
    events = [_ev(0, 1.0, 3.0), _ev(1, 11.0, 14.0), _ev(2, 20.0, 22.0)]
    events, n = apply_cuts_to_events(events, cuts)
    segs = [{**s, "start": shift_time(s["start"], cuts),
             "end": shift_time(s["end"], cuts)} for s in segs]
    assert n == 1
    pairs = build_dub_pairs(segs, events)
    pairs["cuts"] = cuts
    idxs = [r["idx"] for r in pairs["subs"]]
    assert idxs == [0, 2]                            # 컷 안 줄은 다음 카드에서 빠진다
    assert pairs["subs"][1]["start"] == 15.0         # 당겨진 시각으로 동봉
    # retime 후 실측 end 반영이 cuts 메타를 보존한다
    updated = update_pairs_actual_ends(pairs, [dict(events[0], end=3.4)])
    assert updated["cuts"] == cuts
    assert updated["subs"][0]["end"] == 3.4 and updated["subs"][0]["end_actual"] is True


# ── B/BJ 노출: ja_events.json 에 cuts 동봉 ───────────────────────────────
def test_events_json_doc_carries_cuts():
    cuts = validate_cuts([{"start_sec": 1.0, "end_sec": 2.0}])
    ev = {"entry_idx": 0, "start": 0.0, "end": 1.0, "text": "x"}
    doc = events_json_doc("vid", [ev], cuts=cuts)
    assert doc["cuts"] == cuts
    assert "cuts" not in events_json_doc("vid", [ev])   # 미적용 렌더는 키 자체가 없다
