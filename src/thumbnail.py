"""일본어 썸네일 생성 (C-8) — 브랜드 요소 유지, 텍스트만 일본어.

모드 A: 텍스트 영역 좌표에 일본어 카피(persona 톤)를 Pillow 합성.
모드 B: 외부 이미지번역 도구 연동 자리(stub).
캐릭터 비주얼 변형 금지(원본 위에 텍스트만). 로컬 파일까지만(게시 금지).

출력: outputs/{video_id}/thumb_ja_v1.png, v2.png (A/B 후보) + copy_candidates.json
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pathlib import Path  # noqa: E402
from typing import Any, Optional  # noqa: E402

from engine.common import (ensure_dir, get_logger, get_secret, load_config,  # noqa: E402
                           load_persona, resolve_path, write_json)

log = get_logger("thumbnail")


# ── 순수 헬퍼 ─────────────────────────────────────────────────────────────
def build_copy_prompt(source_title: str, n: int) -> str:
    return (
        f"YouTube サムネイル用の短い日本語コピーを{n}案。原題: {source_title}\n"
        "各案12文字以内・一目で刺さる・キャラ性。JSON配列(文字列のみ)で出力。"
    )


def fit_font_size(box_w: int, box_h: int, text: str, max_size: int = 120) -> int:
    """박스에 대략 맞는 폰트 크기 추정(글자수·높이 기준)."""
    if not text:
        return max_size
    by_w = int(box_w / max(1, len(text)) * 1.6)
    by_h = int(box_h * 0.8)
    return max(16, min(max_size, by_w, by_h))


# ── 카피 생성(LLM, 선택) ─────────────────────────────────────────────────
def generate_copy(source_title: str, config: dict[str, Any], n: int = 2) -> list[str]:
    key = get_secret("LLM_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
    if not key:
        log.warning("LLM 키 없음 → 카피 후보는 원제 기반 플레이스홀더. 사람이 작성 권장.")
        return [source_title[:12] or "残念ルーピー", "今日も残念ぴ"][:n]
    from engine import llm

    try:
        raw = llm.complete(load_persona(config), build_copy_prompt(source_title, n),
                           config, max_tokens=256)
    except ImportError:
        log.warning("LLM SDK 미설치 → 플레이스홀더 카피.")
        return [source_title[:12] or "残念ルーピー", "今日も残念ぴ"][:n]
    try:
        from engine.translate import parse_llm_json

        items = [str(x) for x in parse_llm_json(raw)]
        return items[:n] or [source_title[:12]]
    except Exception:  # noqa: BLE001
        return [source_title[:12] or "残念ルーピー"][:n]


# ── 합성 ──────────────────────────────────────────────────────────────────
def compose(base_image: str, copy: str, out_path: str, config: dict[str, Any],
            coords: Optional[tuple[int, int, int, int]] = None) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(base_image).convert("RGB")
    w, h = img.size
    x1, y1, x2, y2 = coords or (int(w * 0.06), int(h * 0.06), int(w * 0.94), int(h * 0.28))
    fonts_dir = resolve_path(config["paths"]["fonts_dir"])
    size = fit_font_size(x2 - x1, y2 - y1, copy)
    try:
        font = ImageFont.truetype(str(fonts_dir / "NotoSansJP-Black.ttf"), size=size)
    except Exception:
        log.warning("썸네일 폰트 로드 실패 → 기본 폰트. fonts/ 확인")
        font = ImageFont.load_default()
    draw = ImageDraw.Draw(img)
    draw.text((x1, y1), copy, font=font, fill=(255, 255, 255),
              stroke_width=max(3, size // 12), stroke_fill=(0, 0, 0))
    out = Path(out_path)
    ensure_dir(out.parent)
    img.save(out)
    return out


def thumbnail(video_id: str, base_image: str, source_title: str, config: dict[str, Any],
              coords: Optional[tuple[int, int, int, int]] = None) -> dict[str, Any]:
    copies = generate_copy(source_title, config, n=2)
    base = resolve_path(f"{config['paths']['outputs_dir']}/{video_id}")
    ensure_dir(base)
    write_json({"_warning": "초벌 카피, 사람 확인", "candidates": copies},
               base / "copy_candidates.json")
    outputs = []
    for i, copy in enumerate(copies, 1):
        outputs.append(str(compose(base_image, copy, str(base / f"thumb_ja_v{i}.png"),
                                   config, coords)))
    log.info("썸네일 후보 %d개(검수 전): %s", len(outputs), outputs)
    return {"thumbnails": outputs, "copies": copies}


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="일본어 썸네일 생성(로컬 파일까지)")
    p.add_argument("--video-id", required=True)
    p.add_argument("--base", required=True, help="원본 썸네일/프레임 이미지")
    p.add_argument("--title", default="", help="원제(카피 생성용)")
    p.add_argument("--coords", nargs=4, type=int, metavar=("X1", "Y1", "X2", "Y2"))
    p.add_argument("--config", default=None)
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    coords = tuple(args.coords) if args.coords else None
    thumbnail(args.video_id, args.base, args.title, load_config(args.config), coords=coords)


if __name__ == "__main__":
    main()
