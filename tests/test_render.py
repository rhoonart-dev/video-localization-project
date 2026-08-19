"""engine/render.py — 폰트 해석 / 래핑 / 타임코드 / ASS·SRT 빌드 / 이벤트 병합."""
from engine.render import (_align_code, ass_timestamp, build_ass, build_bilingual_ass,
                           build_srt, detections_to_events, resolve_font, wrap_text)
from engine.schemas import DetectionDoc, FrameDetections, Region, Style

FONT_MAP = {
    "default": "D.ttf",
    "rules": [
        {"match": {"weight": "bold"}, "jp_font": "Black.ttf"},
        {"match": {"style": "serif"}, "jp_font": "Serif.ttf"},
        {"match": {"size_max": 28}, "jp_font": "Med.ttf"},
    ],
}


def test_resolve_font_bold_rule():
    assert resolve_font(Style(bold=True, font_size=40), FONT_MAP) == "Black.ttf"


def test_resolve_font_size_rule():
    assert resolve_font(Style(bold=False, serif=False, font_size=20), FONT_MAP) == "Med.ttf"


def test_resolve_font_falls_back_to_default():
    assert resolve_font(Style(bold=False, serif=False, font_size=40), FONT_MAP) == "D.ttf"


def test_wrap_text_cjk_by_chars():
    assert wrap_text("あいうえおかきくけこ", 4) == ["あいうえ", "おかきく", "けこ"]


def test_wrap_text_spaced_by_words():
    assert wrap_text("hello world foo", 11) == ["hello world", "foo"]


def test_ass_timestamp():
    assert ass_timestamp(3661.5) == "1:01:01.50"


def test_align_code():
    assert _align_code("bottom-center") == 2
    assert _align_code("top-left") == 7
    assert _align_code("center-right") == 6


def test_build_ass_structure():
    ev = [{"start": 0.0, "end": 1.0, "text": "こんにちは", "position": "bottom-center"}]
    out = build_ass(ev, 1920, 1080, 16)
    assert "PlayResX: 1920" in out and "Dialogue:" in out and "こんにちは" in out


def test_build_srt_manual_fallback():
    ev = [{"start": 0.0, "end": 1.5, "text": "やあ"}]
    out = build_srt(ev, 16)
    assert "00:00:00,000 --> 00:00:01,500" in out and "やあ" in out


def test_detections_to_events_merges_consecutive():
    doc = DetectionDoc(
        video_id="v", fps=30.0, width=100, height=100, sample_every=15, ocr_backend="x",
        frames=[FrameDetections(0, 0.0, [Region((0, 80, 100, 100), "안녕")]),
                FrameDetections(15, 0.5, [Region((0, 80, 100, 100), "안녕")])])
    ev = detections_to_events(doc, {"안녕": "やあ"})
    assert len(ev) == 1 and ev[0]["text"] == "やあ"
    assert ev[0]["end"] > ev[0]["start"]
    assert ev[0]["bbox"] == (0, 80, 100, 100)        # 한국어 위치(일본어 배치용)


def test_build_bilingual_ass_above_uses_pos_and_an2():
    ev = [{"start": 0.0, "end": 1.0, "text": "やあ", "bbox": (300, 600, 900, 660)}]
    out = build_bilingual_ass(ev, 1280, 720, 16, position="above")
    assert "\\pos(600,592)" in out      # 한국어 bbox 위(600 - gap 8), 중앙 x=600
    assert "\\an2" in out               # 하단중앙 앵커 → 텍스트가 위로
    assert "やあ" in out


def test_build_bilingual_ass_below_uses_an8():
    ev = [{"start": 0.0, "end": 1.0, "text": "やあ", "bbox": (300, 100, 900, 160)}]
    out = build_bilingual_ass(ev, 1280, 720, 16, position="below")
    assert "\\an8" in out and "\\pos(600,168)" in out   # bbox 아래(160 + gap 8)


def test_build_ass_margin_v_override():
    from engine.render import build_ass
    events = [{"start": 0.0, "end": 2.0, "text": "ルーピー"}]
    default = build_ass(events, 1920, 1080)
    assert ",20,20,30,1" in default                    # 기본 MarginV=30
    raised = build_ass(events, 1920, 1080, margin_v=124)
    assert ",20,20,124,1" in raised                    # 캡션 회피 배치


# ── 줄 스타일·타이밍 오버라이드 계약(8/20 — docs/subtitle-style-overrides.md) ──
from engine.render import (attach_entry_overrides, events_json_doc, hex_to_ass_color,
                           style_ass_tags, style_margin_v, validate_line_style,
                           validate_line_timing)


def test_validate_line_style_normalizes_and_rejects():
    import pytest
    ok = validate_line_style({"size": 64, "y": 0.8, "color": "#ffdd00", "rotate": -8})
    assert ok == {"size": 64.0, "y": 0.8, "color": "#FFDD00", "rotate": -8.0}
    assert validate_line_style({"color": "#FF0000"}) == {"color": "#FF0000"}  # 부분 지정
    with pytest.raises(ValueError):
        validate_line_style({"fontsize": 12})          # 모르는 키 즉시 거절
    with pytest.raises(ValueError):
        validate_line_style({"size": 0})
    with pytest.raises(ValueError):
        validate_line_style({"y": -0.1})
    with pytest.raises(ValueError):
        validate_line_style({"color": "FFDD00"})       # # 누락
    with pytest.raises(ValueError):
        validate_line_style({"rotate": 181})
    with pytest.raises(ValueError):
        validate_line_style("big")                     # dict 아님


def test_validate_line_timing():
    import pytest
    assert validate_line_timing({"start_sec": 1.5, "end_sec": 4}) == (1.5, 4.0)
    assert validate_line_timing({"ja": "x"}) == (None, None)
    with pytest.raises(ValueError):
        validate_line_timing({"start_sec": "1.5"})     # 문자열 숫자 불허(타입 검증)
    with pytest.raises(ValueError):
        validate_line_timing({"end_sec": -1})
    with pytest.raises(ValueError):
        validate_line_timing({"start_sec": 5.0, "end_sec": 5.0})


def test_style_ass_tags_sign_flip_and_canvas_scale():
    tags = style_ass_tags({"size": 64, "color": "#FFDD00", "rotate": -8}, 1920)
    assert tags == "\\fs64\\1c&H00DDFF&\\frz8"         # BGR + 시계→반시계 부호 반전
    assert style_ass_tags({"rotate": 0}, 1920) == ""   # 0 은 태그를 안 박는다
    # size 는 1080×1920 계약 px → PlayResY 960(절반) 캔버스에선 절반으로 환산
    assert style_ass_tags({"size": 64}, 960) == "\\fs32"
    assert hex_to_ass_color("#102030") == "&H302010&"


def test_style_margin_v_bottom_ratio():
    assert style_margin_v({"y": 0.8}, 1920) == 384     # (1-0.8)*1920
    assert style_margin_v({"y": 1.0}, 1920) == 1       # 최소 1(0 은 스타일 기본값 의미)
    assert style_margin_v({}, 1920) == 0               # 미지정 = 스타일 기본


def test_build_ass_event_style_tags_and_margin():
    ev = [{"start": 0.0, "end": 2.0, "text": "スタイル",
           "style": {"size": 64, "color": "#FFDD00", "rotate": -8, "y": 0.8}},
          {"start": 3.0, "end": 4.0, "text": "そのまま"}]
    out = build_ass(ev, 1080, 1920, 16)
    styled = next(ln for ln in out.splitlines() if "スタイル" in ln)
    plain = next(ln for ln in out.splitlines() if "そのまま" in ln)
    assert "{\\an2\\fs64\\1c&H00DDFF&\\frz8}" in styled
    assert ",0,0,384,," in styled                      # y=0.8 → 이벤트 MarginV(하단 정렬)
    assert "{\\an2}" in plain and ",0,0,0,," in plain  # 무스타일 줄은 종전 그대로


def test_build_ass_event_style_top_alignment_falls_back_to_pos():
    ev = [{"start": 0.0, "end": 1.0, "text": "上", "position": "top-center",
           "style": {"y": 0.25}}]
    out = build_ass(ev, 1080, 1920, 16)
    assert "\\pos(540,480)" in out                     # 상단 정렬 → \pos 폴백(0.25*1920)


def test_build_bilingual_ass_style_y_overrides_auto_placement():
    ev = [{"start": 0.0, "end": 1.0, "text": "やあ", "bbox": (300, 600, 900, 660),
           "style": {"y": 0.5, "size": 40}}]
    out = build_bilingual_ass(ev, 1280, 720, 16, position="above")
    assert "\\an2\\pos(600,360)" in out                # 사람이 정한 y(0.5*720)가 이긴다
    assert "\\fs15" in out                             # 40 * 720/1920 = 15


def test_attach_entry_overrides_by_source_and_first_event_timing():
    entries = [{"source": "안녕", "target": "やあ",
                "style": {"color": "#FF0000"}, "start_sec": 5.0, "end_sec": 7.0},
               {"source": "잘가", "target": "バイバイ"}]
    events = [{"start": 0.0, "end": 1.0, "text": "やあ", "source": "안녕"},
              {"start": 2.0, "end": 3.0, "text": "やあ", "source": "안녕"},
              {"start": 9.0, "end": 9.5, "text": "バイバイ", "source": "잘가"}]
    out = attach_entry_overrides(events, entries)
    first = next(e for e in out if e.get("end_fixed"))
    assert first["start"] == 5.0 and first["end"] == 7.0 and first["entry_idx"] == 0
    # style 은 같은 source 의 모든 이벤트에, 타이밍은 첫 이벤트에만
    same = [e for e in out if e.get("source") == "안녕"]
    assert all(e["style"] == {"color": "#FF0000"} for e in same)
    assert sum(1 for e in same if e.get("end_fixed")) == 1
    assert next(e for e in out if e["source"] == "잘가")["entry_idx"] == 1
    assert [e["start"] for e in out] == sorted(e["start"] for e in out)  # 재정렬 보장


def test_events_json_doc_schema():
    doc = events_json_doc("vid", [{"entry_idx": 3, "start": 2.0, "end": 5.5, "text": "…",
                                   "position": "bottom-center", "bbox": (1, 2, 3, 4),
                                   "style": {"y": 0.5}, "end_fixed": True},
                                  {"start": 6.0, "end": 7.0, "text": "x"}])
    assert doc["video_id"] == "vid" and len(doc["events"]) == 2
    e0, e1 = doc["events"]
    assert e0["bbox"] == [1, 2, 3, 4] and e0["style"] == {"y": 0.5} and e0["end_fixed"]
    assert e1["entry_idx"] is None and e1["style"] is None and e1["end_fixed"] is False


# ── 자가개선: 렌더 OCR 백체크 (2026-07-21) ────────────────────────────────
from engine.render import match_cer, pick_backcheck_frames


def test_match_cer_absorbs_line_splits_and_noise():
    # OCR 이 줄을 쪼개도 전체 연결 후보로 흡수
    assert match_cer("ラーメン最高", ["ラーメン", "最高"]) == 0.0
    assert match_cer("ラーメン最高", ["ラーメン最高"]) == 0.0
    assert match_cer("ラーメン最高", ["全然違う"]) > 0.5
    assert match_cer("", ["아무거나"]) == 0.0          # 기대 텍스트 없음 → 검사 무의미
    assert match_cer("ラーメン", []) == 1.0            # OCR 전무 → 완전 불일치


def test_pick_backcheck_frames_even_sampling():
    assert pick_backcheck_frames([1, 2, 3], 6) == [1, 2, 3]        # 전수
    picked = pick_backcheck_frames(list(range(100)), 6)
    assert len(picked) == 6 and picked[0] == 0                     # 균등 간격
    assert pick_backcheck_frames([1, 2], 0) == []
    assert pick_backcheck_frames([], 6) == []
