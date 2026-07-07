"""engine/llm.py — provider 선택 / 모델 해석 / 분기 라우팅(순수 로직).

프로젝트 러너(tests/run_all.py)는 인자 없는 test_* 함수를 호출하고 pytest 미설치 →
픽스처 없이 평범한 assert + try/finally 로 작성한다.
"""
import os

from engine import llm


def test_provider_defaults_to_gemini():
    assert llm.provider({}) == "gemini"


def test_provider_reads_config_and_lowercases():
    assert llm.provider({"translate": {"provider": "Anthropic"}}) == "anthropic"


def test_resolve_model_prefers_hero_then_base():
    cfg = {"translate": {"model": "m-base", "hero_model": "m-hero"}}
    assert llm.resolve_model(cfg) == "m-base"
    assert llm.resolve_model(cfg, hero=True) == "m-hero"


def test_resolve_model_env_override():
    prev = os.environ.get("LLM_MODEL")
    os.environ["LLM_MODEL"] = "m-env"
    try:
        assert llm.resolve_model({"translate": {"model": "m-base"}}) == "m-env"
    finally:
        os.environ.pop("LLM_MODEL", None)
        if prev is not None:
            os.environ["LLM_MODEL"] = prev


def test_complete_unknown_provider_raises():
    try:
        llm.complete("s", "u", {"translate": {"provider": "openai", "model": "x"}})
    except ValueError:
        return
    raise AssertionError("unknown provider 는 ValueError 여야 함")


def test_complete_missing_model_raises():
    prev = os.environ.pop("LLM_MODEL", None)
    try:
        llm.complete("s", "u", {"translate": {"provider": "gemini"}})
    except ValueError:
        return
    finally:
        if prev is not None:
            os.environ["LLM_MODEL"] = prev
    raise AssertionError("모델 미지정은 ValueError 여야 함")


def test_complete_routes_to_provider():
    """provider 에 맞는 내부 함수로 분기하는지(SDK 없이 검증)."""
    orig_g, orig_a = llm._gemini, llm._anthropic
    called = {}
    llm._gemini = lambda s, u, m, mt: called.setdefault("p", "gemini") or "ok"
    llm._anthropic = lambda s, u, m, mt: called.setdefault("p", "anthropic") or "ok"
    try:
        llm.complete("s", "u", {"translate": {"provider": "gemini", "model": "gemini-x"}})
        assert called["p"] == "gemini"
        called.clear()
        llm.complete("s", "u", {"translate": {"provider": "anthropic", "model": "claude-x"}})
        assert called["p"] == "anthropic"
    finally:
        llm._gemini, llm._anthropic = orig_g, orig_a
