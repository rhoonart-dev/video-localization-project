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
