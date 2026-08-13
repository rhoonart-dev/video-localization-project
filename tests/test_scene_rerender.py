"""scene-rerender 모드 순수 로직 테스트.

실행: python -m pytest tests/test_scene_rerender.py -q
(엔진 자체는 ai-video venv 로 도므로, 이 테스트는 표준 라이브러리만 쓴다.)
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("localize_run", ROOT / "scripts" / "localize_run.py")
lr = importlib.util.module_from_spec(_spec)
sys.modules["localize_run"] = lr
_spec.loader.exec_module(lr)


# ───────── 엔진 경로 (워커에서 깨지지 않는 것이 핵심) ─────────

def test_engine_path_falls_back_to_sibling(monkeypatch):
    """로컬 ~/ves/<engine> · 워커 $VES_HOME/engines/<engine> 둘 다 형제 배치다."""
    monkeypatch.delenv("AI_VIDEO_ROOT", raising=False)
    assert lr.engine_path("AI_VIDEO_ROOT", "ai-video") == ROOT.parent / "ai-video"


def test_engine_path_env_wins(monkeypatch):
    monkeypatch.setenv("AI_VIDEO_ROOT", "/opt/ves/engines/ai-video")
    assert str(lr.engine_path("AI_VIDEO_ROOT", "ai-video")) == "/opt/ves/engines/ai-video"


def test_no_absolute_user_paths_in_source():
    """특정 계정 홈이 박히면 워커에서 100% 실패한다 — 회귀 방지."""
    src = (ROOT / "scripts" / "localize_run.py").read_text(encoding="utf-8")
    assert "/Users/" not in src


# ───────── 재렌더 노브 복원 (컷 재현 = 자막 싱크) ─────────

def _run_log(**app):
    return {"provenance": {"config": {"app": app}}}


def test_render_flags_restores_aggressive_tight():
    f = lr.render_flags(_run_log(silence_cut_profile="aggressive", target_duration_sec=45,
                                max_duration_sec=50, max_duration_tolerance=1.1))
    assert f[:2] == ["--silence-profile", "aggressive"]
    assert "--length-profile" in f and f[f.index("--length-profile") + 1] == "tight"
    assert "--loudness-lufs" in f


def test_render_flags_conservative_standard_has_no_length_flag():
    f = lr.render_flags(_run_log(silence_cut_profile="conservative", target_duration_sec=60,
                                 max_duration_sec=70, max_duration_tolerance=1.5))
    assert f[:2] == ["--silence-profile", "conservative"]
    assert "--length-profile" not in f


def test_render_flags_tolerates_missing_provenance():
    """옛 런(provenance 없음)이라도 죽지 않고 최소한 라우드니스는 준다."""
    f = lr.render_flags({})
    assert "--loudness-lufs" in f


# ───────── 텔롭 병기 트랙 ─────────

def test_build_telop_ass_uses_orig_index_and_skips_unused(tmp_path):
    """L2b 재보정본은 orig_index 로 번역과 짝을 맞춘다(필터로 순번이 밀리므로)."""
    refined = [{"orig_index": 2, "start_sec": 1.0, "end_sec": 2.0, "text_ko": "가"},
               {"orig_index": 5, "start_sec": 3.0, "end_sec": 4.0, "text_ko": "나"}]
    tr = {"telops": [{"index": 2, "use": True, "ja": "アガ"},
                     {"index": 5, "use": False, "ja": "ナ"}]}
    out = tmp_path / "telops.ass"
    assert lr.build_telop_ass(refined, tr, "ArialUnicode", out) == 1
    body = out.read_text(encoding="utf-8")
    assert "アガ" in body and "ナ" not in body
    assert "0:00:01.00,0:00:02.00" in body


def test_build_telop_ass_filters_kind_when_not_refined(tmp_path):
    raw = [{"kind": "our_subtitle", "start_sec": 0.0, "end_sec": 1.0, "text_ko": "우리"},
           {"kind": "broadcast_telop", "start_sec": 1.0, "end_sec": 2.0, "text_ko": "텔롭"}]
    tr = {"telops": [{"index": 0, "use": True, "ja": "テロップ"}]}
    out = tmp_path / "t.ass"
    assert lr.build_telop_ass(raw, tr, "ArialUnicode", out) == 1


def test_ass_escape_and_timestamp():
    assert lr._ass_escape("a\nb") == "a\\Nb"
    assert lr._ass_escape("{x}") == "(x)"          # ASS 오버라이드 블록 무력화
    assert lr._fmt_ts(3661.5) == "1:01:01.50"
