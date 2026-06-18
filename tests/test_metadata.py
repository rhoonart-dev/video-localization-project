"""src/metadata.py — 프롬프트 / JSON 파싱 / 드래프트 조립 순수 로직."""
from src.metadata import (DEFAULT_COPYRIGHT, WARNING, assemble_draft, build_prompt,
                          parse_llm_object)


def test_build_prompt_includes_title_and_glossary():
    p = build_prompt("불닭먹방", "설명", {"불닭": "ブルダック"})
    assert "불닭먹방" in p and "ブルダック" in p


def test_parse_llm_object_with_fence():
    raw = '```json\n{"title_candidates": ["a", "b"]}\n```'
    assert parse_llm_object(raw)["title_candidates"] == ["a", "b"]


def test_assemble_draft_truncates_and_appends_copyright():
    llm = {"title_candidates": ["t1", "t2", "t3", "t4"], "description": "本文",
           "hashtags": ["#x"], "tags": [str(i) for i in range(25)]}
    d = assemble_draft("vid", llm, DEFAULT_COPYRIGHT)
    assert d["_warning"] == WARNING
    assert len(d["title_candidates"]) == 3
    assert len(d["tags"]) == 20
    assert DEFAULT_COPYRIGHT in d["description"]
