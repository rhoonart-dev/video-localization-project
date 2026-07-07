"""[엔진③-a] 번역 + 트랜스크리에이션 (GhostCut 차별 레이어의 절반).

detections.json 의 한국어 텍스트를 일본어로 *트랜스크리에이션*.
- persona.md 를 LLM system 에 주입(캐릭터 톤·어미·표기 규칙).
- glossary.yaml 고정 용어 후처리로 표기 일관성 보장.
- 선택적으로 DeepL 초벌 병용(--deepl) — LLM 이 이를 참고해 다듬음.
- 결과는 **초벌**(draft=True): 네이티브 검수 게이트 전.

LLM/HTTP 는 lazy import. 프롬프트 빌드·glossary 적용·JSON 파싱은 순수 → 테스트 가능.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Optional

from engine.common import (get_logger, get_secret, load_config, load_glossary,
                           load_persona, resolve_path)
from engine.schemas import DetectionDoc, TranslationDoc, TranslationEntry

log = get_logger("translate")


# ── 순수 헬퍼 ─────────────────────────────────────────────────────────────
def build_system_prompt(persona: str, glossary: dict[str, str]) -> str:
    parts = [
        "あなたは韓国アニメ『ジャンマンルーピー(잔망루피)』日本版チャンネルの",
        "字幕トランスクリエーター。以下のペルソナ規則に厳密に従う。\n",
        "=== PERSONA ===", persona.strip(), "=== /PERSONA ===\n",
        "原則: 直訳禁止。意味と感情とミームを日本語の文脈で再創作する。",
        "画面字幕は原文より長くしない。キャラ語尾は PERSONA の規則のみ適用",
        "(未確定なら標準語にし notes に記す)。",
    ]
    if glossary:
        gl = "\n".join(f"- {k} → {v}" for k, v in glossary.items())
        parts += ["\n=== 用語集(固定表記・厳守) ===", gl]
    return "\n".join(parts)


def build_user_prompt(texts: list[str], drafts: Optional[dict[str, str]] = None) -> str:
    lines = [
        "次の韓国語テキストを日本語にトランスクリエーションせよ。",
        '出力は JSON 配列のみ。各要素 {"source","target","notes","flagged"}。',
        "flagged は文脈不足・固有名詞不確実など人手確認が要る場合 true。\n",
    ]
    for i, t in enumerate(texts):
        d = f"  (DeepL下訳: {drafts[t]})" if drafts and t in drafts else ""
        lines.append(f"{i + 1}. {t}{d}")
    return "\n".join(lines)


def apply_glossary(text: str, glossary: dict[str, str]) -> str:
    """번역 결과에 고정 용어집을 후처리로 강제(한국어 원형이 남아있으면 치환)."""
    for ko, ja in glossary.items():
        text = text.replace(ko, ja)
    return text


def parse_llm_json(content: str) -> list[dict[str, Any]]:
    """LLM 응답에서 JSON 배열 추출(코드펜스/잡텍스트 허용)."""
    content = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
    if fence:
        content = fence.group(1).strip()
    start, end = content.find("["), content.rfind("]")
    if start != -1 and end != -1 and end > start:
        content = content[start:end + 1]
    data = json.loads(content)
    if not isinstance(data, list):
        raise ValueError("LLM 응답이 JSON 배열이 아님")
    return data


# ── DeepL 초벌 (선택, requests) ──────────────────────────────────────────
def deepl_draft(texts: list[str], config: dict[str, Any]) -> dict[str, str]:
    key = get_secret("TRANSLATE_API_KEY")
    if not key:
        log.warning("TRANSLATE_API_KEY 없음 → DeepL 초벌 건너뜀")
        return {}
    import requests

    endpoint = config.get("translate", {}).get("deepl_endpoint",
                                                "https://api-free.deepl.com/v2/translate")
    out: dict[str, str] = {}
    for t in texts:
        try:
            r = requests.post(endpoint, data={"auth_key": key, "text": t,
                                              "source_lang": "KO", "target_lang": "JA"}, timeout=30)
            r.raise_for_status()
            out[t] = r.json()["translations"][0]["text"]
        except Exception as e:  # noqa: BLE001
            log.warning("DeepL 실패(%s): %s", t, e)
    return out


# ── LLM 트랜스크리에이션 ─────────────────────────────────────────────────
def transcreate(texts: list[str], config: dict[str, Any], hero: bool = False,
                use_deepl: bool = False) -> list[TranslationEntry]:
    if not texts:
        return []
    persona = load_persona(config)
    glossary = load_glossary(config)
    tcfg = config.get("translate", {})
    from engine import llm
    model = llm.resolve_model(config, hero=hero)

    drafts = deepl_draft(texts, config) if use_deepl else None
    system = build_system_prompt(persona, glossary)
    user = build_user_prompt(texts, drafts)

    log.info("트랜스크리에이션 provider=%s model=%s 텍스트=%d hero=%s deepl=%s",
             llm.provider(config), model, len(texts), hero, use_deepl)
    raw = llm.complete(system, user, config, model=model,
                       max_tokens=int(tcfg.get("max_tokens", 1024)), hero=hero)
    rows = parse_llm_json(raw)

    by_source = {r.get("source"): r for r in rows}
    entries: list[TranslationEntry] = []
    for t in texts:  # 입력 순서·완전성 보장
        r = by_source.get(t, {})
        target = apply_glossary(r.get("target", ""), glossary)
        entries.append(TranslationEntry(source=t, target=target,
                                        notes=r.get("notes", ""),
                                        flagged=bool(r.get("flagged", not target))))
    return entries


def translate(detections_path: str, config: dict[str, Any], hero: bool = False,
              use_deepl: bool = False, out_path: Optional[str] = None) -> TranslationDoc:
    doc = DetectionDoc.load(detections_path)
    texts = doc.unique_texts()
    from engine import llm
    model = llm.resolve_model(config, hero=hero)
    entries = transcreate(texts, config, hero=hero, use_deepl=use_deepl)
    tdoc = TranslationDoc(video_id=doc.video_id, model=model or "unknown", draft=True, entries=entries)

    out = Path(out_path) if out_path else resolve_path(
        f"{config['paths']['outputs_dir']}/{doc.video_id}/translations.json")
    tdoc.save(out)
    flagged = sum(1 for e in entries if e.flagged)
    log.info("번역 초벌 저장(검수 전): %s (항목 %d, 검수필요 %d)", out, len(entries), flagged)
    return tdoc


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="한국어 → 일본어 트랜스크리에이션(초벌)")
    p.add_argument("--detections", required=True, help="detections.json 경로")
    p.add_argument("--config", default=None)
    p.add_argument("--hero", action="store_true", help="고품질 모델(hero_model) 사용")
    p.add_argument("--deepl", action="store_true", help="DeepL 초벌 병용")
    p.add_argument("--out", default=None)
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    config = load_config(args.config)
    translate(args.detections, config, hero=args.hero, use_deepl=args.deepl, out_path=args.out)


if __name__ == "__main__":
    main()
