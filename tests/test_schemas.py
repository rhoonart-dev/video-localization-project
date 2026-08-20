"""engine/schemas.py — dataclass 직렬화/역직렬화 계약."""
from engine.schemas import (DetectionDoc, FrameDetections, Region, Style,
                            TranslationDoc, TranslationEntry)


def test_style_from_dict_defaults():
    s = Style.from_dict({})
    assert s.color == (255, 255, 255)
    assert s.font_size == 32 and s.bold is True


def test_style_from_dict_values():
    s = Style.from_dict({"color": [10, 20, 30], "font_size": 50, "serif": True})
    assert s.color == (10, 20, 30) and s.font_size == 50 and s.serif is True


def test_region_from_dict():
    r = Region.from_dict({"bbox": [1, 2, 3, 4], "text": "안녕", "confidence": 0.9})
    assert r.bbox == (1, 2, 3, 4) and r.text == "안녕" and r.confidence == 0.9


def test_detectiondoc_unique_texts_preserves_order_and_dedups():
    doc = DetectionDoc(
        video_id="v", fps=30.0, width=100, height=100, sample_every=15, ocr_backend="x",
        frames=[
            FrameDetections(0, 0.0, [Region((0, 0, 1, 1), "A"), Region((0, 0, 1, 1), "B")]),
            FrameDetections(15, 0.5, [Region((0, 0, 1, 1), "A"), Region((0, 0, 1, 1), "C")]),
        ])
    assert doc.unique_texts() == ["A", "B", "C"]


def test_detectiondoc_roundtrip():
    doc = DetectionDoc(
        video_id="v", fps=30.0, width=10, height=20, sample_every=15, ocr_backend="rapidocr",
        roi=(1, 2, 3, 4),
        frames=[FrameDetections(0, 0.0, [Region((0, 0, 5, 5), "안녕", 0.8)])])
    back = DetectionDoc.from_dict(doc.to_dict())
    assert back.roi == (1, 2, 3, 4)
    assert back.frames[0].regions[0].text == "안녕"
    assert back.frames[0].regions[0].style.color == (255, 255, 255)


def test_translationdoc_as_map():
    t = TranslationDoc(video_id="v", model="m",
                       entries=[TranslationEntry("안녕", "こんにちは"),
                                TranslationEntry("불닭", "ブルダック")])
    assert t.as_map() == {"안녕": "こんにちは", "불닭": "ブルダック"}
    assert t.draft is True


def test_translation_entry_use_roundtrip_and_asmap_excludes():
    """자막 소프트 삭제(E6-0): use=false 는 tmap 에서 빠져 번인·ass/srt·ja_events 가
    함께 빠진다. 재기록(to_dict)에도 살아남아야 재렌더가 멱등이다."""
    doc = TranslationDoc.from_dict({
        "video_id": "v",
        "entries": [{"source": "하나", "target": "一", "use": False},
                    {"source": "둘", "target": "二"}]})
    assert doc.entries[0].use is False and doc.entries[1].use is True
    assert doc.as_map() == {"둘": "二"}
    assert doc.to_dict()["entries"][0]["use"] is False
