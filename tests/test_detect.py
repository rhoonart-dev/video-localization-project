"""engine/detect.py — 순수 헬퍼(OCR 백엔드 미설치에서도 동작)."""
from engine.detect import (clamp_bbox, estimate_style, korean_ocr_warning, make_ocr,
                           position_bucket, quad_to_bbox)


def test_korean_ocr_warning_for_rapidocr():
    assert "paddleocr" in korean_ocr_warning("rapidocr", ["korean", "en"])


def test_korean_ocr_warning_none_for_paddle_or_no_korean():
    assert korean_ocr_warning("paddleocr", ["korean"]) == ""
    assert korean_ocr_warning("rapidocr", ["en"]) == ""


def test_quad_to_bbox():
    assert quad_to_bbox([[10, 20], [30, 20], [30, 40], [10, 40]]) == (10, 20, 30, 40)


def test_position_bucket_corners():
    assert position_bucket((0, 0, 10, 10), 300, 300) == "top-left"
    assert position_bucket((140, 140, 160, 160), 300, 300) == "center-center"
    assert position_bucket((280, 280, 300, 300), 300, 300) == "bottom-right"


def test_clamp_bbox():
    assert clamp_bbox((-5, -5, 400, 400), 100, 100) == (0, 0, 100, 100)


def test_make_ocr_unknown_raises_value_error():
    raised = False
    try:
        make_ocr("nope")
    except ValueError:
        raised = True
    assert raised


def test_estimate_style_without_numpy_defaults_white():
    # numpy 미설치 → 색은 흰색 기본, 폰트 크기는 bbox 높이.
    s = estimate_style(None, (0, 10, 50, 40), 100, 100)
    assert s.color == (255, 255, 255)
    assert s.font_size == 30
    assert s.position == "top-left"
