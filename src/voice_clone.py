"""ElevenLabs IVC(즉석 클로닝) 부트스트랩 — 분리 보컬에서 레퍼런스 조립 → voice_id 확보.

elevenlabs 백엔드(src/dub.py)는 voice_id 를 요구하지만 만드는 절차가 수동이었다.
이 모듈은 demucs 등으로 분리한 보컬 스템에서 **실제 발화 구간**(자막/ASR 타이밍)만
이어 붙여 레퍼런스를 만들고, voices/add(IVC) 로 보이스를 생성해 voice_id 를 저장한다.

맥6 실증(2026-08-05, 13편 게시): 발화 43세그 ≈170초 레퍼런스로 클로닝, 보이스 설정
stability 0.35 / similarity_boost 1.0 / style 0.6 이 사용자 A/B/C 청취 비교에서 채택됨
(→ recommended_voice_settings, dub.eleven_voice_settings 로 주입).

⚠ 성우 음성 클로닝은 캐릭터 라이선스와 별개로 음성 권리 확인이 필요할 수 있다.
   기본 백엔드(gptsovits)와 달리 이 모듈은 명시적 opt-in 으로만 사용한다.

순수(테스트 대상): plan_reference_segments / recommended_voice_settings / upload_size_ok
외부 의존(ffmpeg/REST)은 build_reference / ensure_voice 에만 격리, lazy import.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pathlib import Path  # noqa: E402

from engine.common import ensure_dir, ffmpeg_bin, get_logger, get_secret, load_config, resolve_path  # noqa: E402

log = get_logger("voice_clone")

API = "https://api.elevenlabs.io/v1"
UPLOAD_LIMIT_BYTES = 11 * 1024 * 1024      # voices/add 파일당 한도(11MB) — 실측 400 응답
DEFAULT_VOICE_NAME = "Zanmang Loopy JA"
STATE_FILE = "voice_clone.json"            # outputs/ 아래 — voice_id 저장(시크릿 아님)


# ── 순수 헬퍼 ─────────────────────────────────────────────────────────────
def recommended_voice_settings() -> dict[str, Any]:
    """맥6 청취 비교(A/B/C, 2026-08-05)에서 채택된 설정 A."""
    return {"stability": 0.35, "similarity_boost": 1.0, "style": 0.6,
            "use_speaker_boost": True}


def plan_reference_segments(items: list[dict[str, Any]], min_dur: float = 1.0,
                            max_total: float = 240.0) -> list[dict[str, Any]]:
    """(보컬 스템 경로, 발화 타이밍) 목록 → 레퍼런스 조립 계획.

    items: [{"path": str, "segments": [{"start": s, "end": e}, …]}, …]
    반환: [{"path", "start", "dur"}] — min_dur 미만 세그 제외, 순서 보존,
    누적 max_total 초과분은 잘라낸다(IVC 는 1-3분이면 충분, 업로드 한도 회피).
    """
    plan: list[dict[str, Any]] = []
    total = 0.0
    for it in items:
        for seg in it.get("segments") or []:
            dur = float(seg["end"]) - float(seg["start"])
            if dur < min_dur:
                continue
            if total + dur > max_total:
                return plan
            plan.append({"path": str(it["path"]), "start": float(seg["start"]),
                         "dur": round(dur, 3)})
            total += dur
    return plan


def upload_size_ok(size_bytes: int, limit: int = UPLOAD_LIMIT_BYTES) -> bool:
    return 0 < size_bytes <= limit


# ── 외부 의존 (ffmpeg / REST) ─────────────────────────────────────────────
def build_reference(plan: list[dict[str, Any]], out_path: str,
                    loudnorm_i: float = -18.0) -> str:
    """계획대로 발화 구간을 이어 붙여 mono 44.1k 레퍼런스 생성(ffmpeg)."""
    import subprocess

    if not plan:
        raise ValueError("레퍼런스 계획이 비어 있음 — plan_reference_segments 결과 확인")
    ensure_dir(Path(out_path).parent)
    inputs: list[str] = []
    labels: list[str] = []
    for i, seg in enumerate(plan):
        inputs += ["-ss", f"{seg['start']:.2f}", "-t", f"{seg['dur']:.2f}",
                   "-i", seg["path"]]
        labels.append(f"[{i}:a]")
    fc = ("".join(labels) + f"concat=n={len(plan)}:v=0:a=1,"
          f"loudnorm=I={loudnorm_i},aresample=44100[out]")
    cmd = [ffmpeg_bin(), "-y", "-loglevel", "error", *inputs,
           "-filter_complex", fc, "-map", "[out]", "-ac", "1", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"레퍼런스 조립 실패: {r.stderr[-500:]}")
    log.info("레퍼런스 조립: %d세그 → %s", len(plan), out_path)
    return out_path


def _compress_if_needed(path: str) -> tuple[str, str]:
    """업로드 한도 초과 시 mp3 192k 로 압축. (경로, mime) 반환."""
    import subprocess

    p = Path(path)
    if upload_size_ok(p.stat().st_size):
        return str(p), "audio/wav" if p.suffix == ".wav" else "audio/mpeg"
    mp3 = p.with_suffix(".upload.mp3")
    r = subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-i", str(p),
                        "-b:a", "192k", str(mp3)], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"레퍼런스 압축 실패: {r.stderr[-300:]}")
    if not upload_size_ok(mp3.stat().st_size):
        raise RuntimeError("압축 후에도 11MB 초과 — max_total 을 줄여 레퍼런스를 짧게")
    return str(mp3), "audio/mpeg"


def ensure_voice(config: dict[str, Any], reference: str,
                 name: str = DEFAULT_VOICE_NAME,
                 state_path: Optional[str] = None) -> str:
    """IVC 보이스 voice_id 반환 — 저장분이 유효하면 재사용, 없으면 생성 후 저장."""
    import requests

    key = get_secret("ELEVENLABS_API_KEY", required=True)
    state = Path(state_path) if state_path else resolve_path(f"outputs/{STATE_FILE}")
    if state.exists():
        vid = json.loads(state.read_text(encoding="utf-8")).get("voice_id", "")
        if vid:
            r = requests.get(f"{API}/voices/{vid}", headers={"xi-api-key": key}, timeout=30)
            if r.ok:
                return vid
            log.warning("저장된 voice_id 무효(%s) — 재생성", r.status_code)

    up_path, mime = _compress_if_needed(reference)
    with open(up_path, "rb") as f:
        r = requests.post(
            f"{API}/voices/add",
            headers={"xi-api-key": key},
            data={"name": name, "description": "IVC bootstrap (src/voice_clone.py)"},
            files={"files": (Path(up_path).name, f, mime)},
            timeout=180,
        )
    if not r.ok:
        raise RuntimeError(f"보이스 생성 실패 {r.status_code}: {r.text[:300]}")
    vid = r.json()["voice_id"]
    ensure_dir(state.parent)
    state.write_text(json.dumps({"voice_id": vid, "name": name}, ensure_ascii=False),
                     encoding="utf-8")
    log.info("IVC 보이스 생성: %s (%s)", vid, name)
    return vid


# ── CLI ───────────────────────────────────────────────────────────────────
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="voice_clone",
                                 description="ElevenLabs IVC 부트스트랩 (opt-in)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="발화 세그 계획(json)으로 레퍼런스 조립")
    p.add_argument("--plan", required=True,
                   help='[{"path","segments":[{"start","end"}]}] 형식 json')
    p.add_argument("--out", required=True)

    p = sub.add_parser("ensure", help="레퍼런스로 IVC 보이스 확보(있으면 재사용)")
    p.add_argument("--reference", required=True)
    p.add_argument("--name", default=DEFAULT_VOICE_NAME)

    args = ap.parse_args(argv)
    config = load_config()
    if args.cmd == "build":
        items = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        plan = plan_reference_segments(items)
        build_reference(plan, args.out)
    else:
        vid = ensure_voice(config, args.reference, name=args.name)
        print(vid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
