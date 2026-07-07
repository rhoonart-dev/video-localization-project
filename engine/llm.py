"""LLM 프로바이더 추상화 — gemini | anthropic. translate/metadata/thumbnail 공용.

config.translate.provider 로 백엔드 선택. 키는 LLM_API_KEY(공용) 우선, 프로바이더별
폴백(ANTHROPIC_API_KEY / GEMINI_API_KEY / GOOGLE_API_KEY)도 인식. SDK 는 lazy import.
"""
from __future__ import annotations

import os
from typing import Optional

from engine.common import get_secret, get_logger

log = get_logger("llm")


def provider(config: dict) -> str:
    return str(config.get("translate", {}).get("provider", "gemini")).lower()


def resolve_model(config: dict, hero: bool = False) -> Optional[str]:
    """LLM_MODEL(env) > config.translate.hero_model/model 순서."""
    tcfg = config.get("translate", {})
    return os.environ.get("LLM_MODEL") or (tcfg.get("hero_model") if hero else tcfg.get("model"))


def complete(system: str, user: str, config: dict, *, model: Optional[str] = None,
             max_tokens: int = 1024, hero: bool = False) -> str:
    """system+user 단일 턴 호출 → 응답 텍스트. provider 에 따라 분기."""
    prov = provider(config)
    model = model or resolve_model(config, hero=hero)
    if not model:
        raise ValueError("LLM 모델 미지정. config.translate.model 또는 LLM_MODEL 설정 필요.")
    if prov == "gemini":
        return _gemini(system, user, model, max_tokens)
    if prov == "anthropic":
        return _anthropic(system, user, model, max_tokens)
    raise ValueError(f"알 수 없는 LLM provider: {prov} (gemini|anthropic)")


def _gemini(system: str, user: str, model: str, max_tokens: int) -> str:
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise ImportError("google-genai 필요: pip install google-genai") from e
    client = genai.Client(api_key=get_secret("LLM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                                             required=True))
    cfg_kwargs = {"system_instruction": system, "max_output_tokens": max_tokens}
    if "flash" in model:    # flash 2.5: thinking 비활성화(출력 토큰 절약·잘림 방지)
        cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    resp = client.models.generate_content(
        model=model, contents=user, config=types.GenerateContentConfig(**cfg_kwargs))
    return resp.text or ""


def _anthropic(system: str, user: str, model: str, max_tokens: int) -> str:
    try:
        import anthropic
    except ImportError as e:
        raise ImportError("anthropic 필요: pip install anthropic") from e
    client = anthropic.Anthropic(api_key=get_secret("LLM_API_KEY", "ANTHROPIC_API_KEY",
                                                    required=True))
    resp = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                  messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
