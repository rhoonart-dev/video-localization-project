"""일본어 메타데이터 생성 (C-3) — 제목/설명/태그/해시태그 트랜스크리에이션.

persona.md 규칙으로 영상별 일본어 메타데이터 *초벌* 생성 → metadata_draft.json.
LLM system 에 persona 주입. 제목 후보 3개, 설명+해시태그+© 라인, 태그 15~20개.
게시 금지(드래프트까지만). 네이티브 검수 전 경고를 JSON 상단에 박는다.

프롬프트 빌드·JSON 파싱은 순수. LLM 호출만 anthropic(lazy).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pathlib import Path  # noqa: E402
from typing import Any, Optional  # noqa: E402

from engine.common import (get_logger, get_secret, load_config, load_glossary,  # noqa: E402
                           load_persona, resolve_path, write_json)

log = get_logger("metadata")

WARNING = "초벌·네이티브 검수 전 — 게시 금지. 제목/표기/해시태그 사람 확인 필수."
DEFAULT_COPYRIGHT = "© ICONIX / OCON / EBS / SKbroadband"


# ── 순수 헬퍼 ─────────────────────────────────────────────────────────────
def build_prompt(source_title: str, source_desc: str, glossary: dict[str, str]) -> str:
    gl = ("\n固定表記: " + ", ".join(f"{k}→{v}" for k, v in glossary.items())) if glossary else ""
    return (
        "次の韓国語動画メタデータを、ペルソナ規則に従い日本チャンネル用に"
        "トランスクリエーションせよ。\n"
        f"原題: {source_title}\n原説明: {source_desc or '(なし)'}{gl}\n\n"
        "出力は JSON オブジェクトのみ:\n"
        '{"title_candidates": [3案], "description": "本文+改行+ハッシュタグ", '
        '"hashtags": [#タグ...], "tags": [検索タグ15〜20]}\n'
        "制約: タイトルは惹き+キャラ性, 説明は共感トーン, "
        "ハッシュタグに #残念ルーピー #ルーピー を含める。"
    )


def parse_llm_object(content: str) -> dict[str, Any]:
    """LLM 응답에서 JSON 오브젝트 추출(코드펜스/잡텍스트 허용)."""
    content = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
    if fence:
        content = fence.group(1).strip()
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end != -1 and end > start:
        content = content[start:end + 1]
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("LLM 응답이 JSON 오브젝트가 아님")
    return data


def assemble_draft(video_id: str, llm: dict[str, Any], copyright_line: str) -> dict[str, Any]:
    """LLM 결과 → 저장용 드래프트(경고/©/필드 정규화)."""
    desc = (llm.get("description") or "").rstrip()
    if copyright_line and copyright_line not in desc:
        desc = f"{desc}\n\n{copyright_line}"
    return {
        "_warning": WARNING,
        "video_id": video_id,
        "title_candidates": (llm.get("title_candidates") or [])[:3],
        "description": desc,
        "hashtags": llm.get("hashtags") or [],
        "tags": (llm.get("tags") or [])[:20],
        "copyright": copyright_line,
    }


# ── 생성 ──────────────────────────────────────────────────────────────────
def generate(video_id: str, source_title: str, source_desc: str, config: dict[str, Any],
             hero: bool = False, out_path: Optional[str] = None) -> Path:
    persona = load_persona(config)
    glossary = load_glossary(config)
    tcfg = config.get("translate", {})
    import os
    model = os.environ.get("LLM_MODEL") or (tcfg.get("hero_model") if hero else tcfg.get("model"))

    try:
        import anthropic
    except ImportError as e:
        raise ImportError("anthropic 필요: pip install anthropic") from e
    client = anthropic.Anthropic(api_key=get_secret("LLM_API_KEY", "ANTHROPIC_API_KEY", required=True))
    log.info("메타데이터 생성 model=%s video_id=%s", model, video_id)
    resp = client.messages.create(
        model=model, max_tokens=int(tcfg.get("max_tokens", 1024)),
        system="あなたはYouTube日本市場のメタデータ最適化担当。\n" + persona.strip(),
        messages=[{"role": "user", "content": build_prompt(source_title, source_desc, glossary)}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    draft = assemble_draft(video_id, parse_llm_object(raw), DEFAULT_COPYRIGHT)

    out = Path(out_path) if out_path else resolve_path(
        f"{config['paths']['outputs_dir']}/{video_id}/metadata_draft.json")
    write_json(draft, out)
    log.info("메타데이터 초벌 저장(검수 전): %s", out)
    return out


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="일본어 메타데이터 초벌 생성(게시 금지)")
    p.add_argument("--video-id", required=True)
    p.add_argument("--title", required=True, help="원본(한국어) 제목")
    p.add_argument("--desc", default="", help="원본 설명")
    p.add_argument("--hero", action="store_true")
    p.add_argument("--config", default=None)
    p.add_argument("--out", default=None)
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    generate(args.video_id, args.title, args.desc, load_config(args.config),
             hero=args.hero, out_path=args.out)


if __name__ == "__main__":
    main()
