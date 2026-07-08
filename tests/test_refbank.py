"""src/refbank.py — 음향 프로필 거리·최적 선택·은행 IO 순수 로직 (오디오/네트워크 없음)."""
import json
import tempfile
from pathlib import Path

from src.refbank import (bank_status, choose_best, hangul_chars, load_bank,
                         profile_distance, spectral_centroid)


def test_hangul_chars():
    assert hangul_chars("루피 먹방") == 4
    assert hangul_chars("ルーピー 123") == 0
    assert hangul_chars("") == 0


def test_spectral_centroid_pure_tone():
    import numpy as np
    sr = 16000
    t = np.arange(sr) / sr
    lo = np.sin(2 * np.pi * 300 * t)
    hi = np.sin(2 * np.pi * 3000 * t)
    assert abs(spectral_centroid(lo, sr) - 300) < 30
    assert spectral_centroid(hi, sr) > spectral_centroid(lo, sr)   # 밝을수록 centroid↑
    assert spectral_centroid([], sr) == 0.0


def test_profile_distance_pitch_and_brightness():
    # 같은 프로필 = 0
    p = {"f0": 405, "centroid": 2272}
    assert profile_distance(p, p) == 0.0
    # 피치 한 옥타브 차 = 1.0 (밝기 동일)
    assert abs(profile_distance({"f0": 400, "centroid": 2000},
                                {"f0": 200, "centroid": 2000}) - 1.0) < 1e-9
    # 밝기 차는 가중치만큼 반영
    d = profile_distance({"f0": 400, "centroid": 4000},
                         {"f0": 400, "centroid": 2000}, brightness_weight=0.7)
    assert abs(d - 0.7) < 1e-9
    # 측정 불가(0) 성분 → inf
    assert profile_distance({"f0": 0, "centroid": 2000}, p) == float("inf")


def test_choose_best_matches_closest_and_excludes_source():
    target = {"f0": 405, "centroid": 2272}         # 커몬2 원본 프로필
    entries = [
        {"wav": "a.wav", "source": "adobe", "f0": 405, "centroid": 3766},   # 밝기 멀다
        {"wav": "b.wav", "source": "ep7", "f0": 400, "centroid": 2300},     # 가장 가깝다
        {"wav": "c.wav", "source": "self", "f0": 405, "centroid": 2272},    # 완벽하지만 제외
    ]
    # self(자기 영상) 제외 → b 선택
    best = choose_best(target, entries, exclude_source="self")
    assert best["wav"] == "b.wav" and "_distance" in best
    # 제외 없으면 c(자기 자신)가 최근접
    assert choose_best(target, entries)["wav"] == "c.wav"
    # 빈 은행 → None
    assert choose_best(target, []) is None


def test_load_bank_and_status_skip_missing_wav():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        cfg = {"dub": {"refbank": {"dir": str(d)}}}
        # 유효 항목(wav 실제 존재)
        (d / "ep7_a.wav").write_bytes(b"x")
        (d / "ep7_a.json").write_text(json.dumps(
            {"wav": str(d / "ep7_a.wav"), "transcript": "안녕", "source": "ep7",
             "f0": 400, "centroid": 2300}), encoding="utf-8")
        # wav 없는 사이드카 → 무시
        (d / "gone.json").write_text(json.dumps(
            {"wav": str(d / "gone.wav"), "transcript": "x", "source": "z"}), encoding="utf-8")
        bank = load_bank(cfg)
        assert len(bank) == 1 and bank[0]["source"] == "ep7"
        assert bank_status(cfg) == {"clips": 1, "sources": ["ep7"]}


def test_load_bank_empty_when_no_dir():
    cfg = {"dub": {"refbank": {"dir": "/nonexistent/refbank/xyz"}}}
    assert load_bank(cfg) == []
    assert bank_status(cfg) == {"clips": 0, "sources": []}
