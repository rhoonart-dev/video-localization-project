"""convert_short 순수부 — edit_plan 좌표 변환·나레이션 구간·필터 문자열."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.convert_short import (blur_filter, canvas_size, collect_texts,  # noqa: E402
                               ja_events, mute_expr, narration_spans, output_timeline)

PLAN = {
    "layout": {"canvas": "1080x1920", "top_title": "김고은의 어린 시절"},
    "timeline": [
        {"clip_start_sec": 100.0, "clip_end_sec": 104.0, "subtitle": "안녕하세요",
         "use_original_audio": True},
        {"clip_start_sec": 300.0, "clip_end_sec": 303.0, "subtitle": "여기서 반전이",
         "use_original_audio": False},
        {"clip_start_sec": 310.0, "clip_end_sec": 312.0, "subtitle": "놀랍게도",
         "use_original_audio": False},
        {"clip_start_sec": 50.0, "clip_end_sec": 55.0, "subtitle": "안녕하세요",
         "use_original_audio": True},
    ],
}


def test_output_timeline_is_cumulative_not_source_coords():
    """clip_*_sec 은 원본 방송 좌표 — 출력 쇼츠 좌표는 누적합이어야 한다."""
    tl = output_timeline(PLAN)
    assert [(c["start"], c["end"]) for c in tl] == [(0.0, 4.0), (4.0, 7.0), (7.0, 9.0), (9.0, 14.0)]
    assert [c["narration"] for c in tl] == [False, True, True, False]


def test_narration_spans_merge_adjacent():
    tl = output_timeline(PLAN)
    assert narration_spans(tl) == [(4.0, 9.0)]          # 4~7, 7~9 인접 → 병합
    assert narration_spans([]) == []


def test_collect_texts_dedup_keeps_order():
    tl = output_timeline(PLAN)
    texts = collect_texts(PLAN, tl)
    assert texts == ["김고은의 어린 시절", "안녕하세요", "여기서 반전이", "놀랍게도"]


def test_ja_events_title_spans_full_and_falls_back_to_source():
    tl = output_timeline(PLAN)
    ja = {"김고은의 어린 시절": "キム・ゴウンの幼少期", "안녕하세요": "こんにちは"}
    ev = ja_events(tl, ja, "김고은의 어린 시절", tl[-1]["end"])
    assert ev[0] == {"start": 0.0, "end": 14.0, "text": "キム・ゴウンの幼少期",
                     "position": "top-center"}
    assert ev[1]["text"] == "こんにちは" and ev[1]["position"] == "bottom-center"
    assert ev[2]["text"] == "여기서 반전이"       # 번역 실패 → 원문 유지(빈 자막 금지)


def test_canvas_and_filters():
    assert canvas_size(PLAN) == (1080, 1920)
    assert canvas_size({}) == (1080, 1920)
    f = blur_filter([(0, 76, 1080, 230), (0, 1382, 1080, 307)])
    assert f.count("boxblur") == 2 and f.endswith("[v]") and "[0:v]split" in f
    assert blur_filter([]) == "[0:v]null[v]"
    assert mute_expr([(4.0, 9.0)]) == "between(t,4.000,9.000)"
