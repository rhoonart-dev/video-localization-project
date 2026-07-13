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


def test_dub_prompt_discourages_katakana_pileup():
    # 더빙 모드(char_budgets)에선 가타카나 복합어 나열 금지 지시가 들어가야 한다.
    from engine.translate import build_user_prompt
    p = build_user_prompt(["마라엽떡 주세요"], char_budgets=[20])
    assert "カタカナ複合語" in p and "羅列" in p
    assert "[≤20文字]" in p
    # 자막 모드(예산 없음)엔 없음 — 캡션은 충실 번역 유지
    p2 = build_user_prompt(["마라엽떡 주세요"])
    assert "カタカナ複合語" not in p2
