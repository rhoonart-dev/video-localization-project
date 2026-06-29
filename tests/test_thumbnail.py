"""src/thumbnail.py — 카피 프롬프트 / 폰트 크기 추정 순수 로직."""
from src.thumbnail import build_copy_prompt, fit_font_size


def test_build_copy_prompt_includes_title_and_count():
    p = build_copy_prompt("불닭먹방", 2)
    assert "불닭먹방" in p and "2" in p


def test_fit_font_size_respects_max():
    assert fit_font_size(400, 100, "あ", max_size=120) <= 120


def test_fit_font_size_floor():
    assert fit_font_size(10, 10, "あいうえお") >= 16
