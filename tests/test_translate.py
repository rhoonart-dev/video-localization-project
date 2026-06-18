"""engine/translate.py — 프롬프트 빌드 / glossary / JSON 파싱 순수 로직."""
from engine.translate import (apply_glossary, build_system_prompt, build_user_prompt,
                              parse_llm_json)


def test_system_prompt_includes_persona_and_glossary():
    s = build_system_prompt("PERSONA_BODY_X", {"불닭": "ブルダック"})
    assert "PERSONA_BODY_X" in s and "ブルダック" in s


def test_user_prompt_numbered_with_deepl_draft():
    u = build_user_prompt(["안녕", "불닭"], {"불닭": "ブルダック"})
    assert "1. 안녕" in u and "2. 불닭" in u and "DeepL" in u


def test_apply_glossary_replaces_source_terms():
    assert apply_glossary("これは불닭です", {"불닭": "ブルダック"}) == "これはブルダックです"


def test_parse_llm_json_with_code_fence():
    raw = '```json\n[{"source": "a", "target": "b"}]\n```'
    out = parse_llm_json(raw)
    assert out[0]["target"] == "b"


def test_parse_llm_json_embedded_in_noise():
    raw = 'noise before [{"source": "a", "target": "b"}] noise after'
    assert parse_llm_json(raw)[0]["source"] == "a"
