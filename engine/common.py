"""공용 유틸 — 설정/시크릿 로딩, 경로, 로깅, ffmpeg 헬퍼.

설계 원칙:
- 무거운 의존성(numpy/cv2/torch/yaml/anthropic ...)은 **함수 안에서 lazy import**.
  → 의존성 미설치 환경에서도 모듈 import 와 --help, 순수 로직 테스트가 동작한다.
- ffmpeg 는 시스템 CLI(subprocess)로 호출(가장 견고). 설치 필요: `ffmpeg`, `ffprobe`.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

# ── 경로 ───────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resolve_path(p: str | os.PathLike, base: Path = PROJECT_ROOT) -> Path:
    """상대경로면 base(기본=프로젝트 루트) 기준으로 절대화."""
    path = Path(p)
    return path if path.is_absolute() else (base / path)


def ensure_dir(p: str | os.PathLike) -> Path:
    d = Path(p)
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_text(p: str | os.PathLike) -> str:
    return Path(p).read_text(encoding="utf-8")


# ── 로깅 ───────────────────────────────────────────────────────────────────
def get_logger(name: str = "loopyjp") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(h)
        logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
        logger.propagate = False
    return logger


# ── 시크릿 / .env ──────────────────────────────────────────────────────────
def load_env(env_path: Optional[str | os.PathLike] = None) -> None:
    """.env 를 환경에 로드. python-dotenv 있으면 사용, 없으면 KEY=VALUE 수동 파싱."""
    path = Path(env_path) if env_path else (PROJECT_ROOT / ".env")
    try:  # pragma: no cover - 의존성 분기
        from dotenv import load_dotenv

        load_dotenv(path if path.exists() else None)
        return
    except Exception:
        pass
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def get_secret(name: str, *fallbacks: str, required: bool = False) -> Optional[str]:
    """환경변수에서 시크릿 조회. 여러 이름 폴백 지원."""
    load_env()
    for key in (name, *fallbacks):
        val = os.environ.get(key)
        if val:
            return val
    if required:
        raise RuntimeError(
            f"시크릿 '{name}' 미설정. .env 에 추가하세요 (.env.example 참고)."
        )
    return None


# ── 설정 ───────────────────────────────────────────────────────────────────
def load_config(path: Optional[str | os.PathLike] = None) -> dict[str, Any]:
    """pipeline.config.yaml 로드 → dict. (pyyaml 필요)"""
    cfg_path = resolve_path(path) if path else (PROJECT_ROOT / "config" / "pipeline.config.yaml")
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise ImportError("pyyaml 필요: pip install pyyaml") from e
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_yaml(path: str | os.PathLike) -> Any:
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise ImportError("pyyaml 필요: pip install pyyaml") from e
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_persona(config: dict[str, Any]) -> str:
    """persona.md 본문 반환(LLM system 주입용)."""
    p = resolve_path(config.get("paths", {}).get("persona", "config/persona.md"))
    return read_text(p) if p.exists() else ""


def load_glossary(config: dict[str, Any]) -> dict[str, str]:
    """glossary.yaml 의 terms 맵 반환(없으면 빈 dict)."""
    rel = config.get("paths", {}).get("glossary")
    if not rel:
        return {}
    p = resolve_path(rel)
    if not p.exists():
        return {}
    data = load_yaml(p) or {}
    return data.get("terms", {}) or {}


# ── JSON IO ────────────────────────────────────────────────────────────────
def write_json(obj: Any, path: str | os.PathLike) -> Path:
    path = Path(path)
    ensure_dir(path.parent)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_json(path: str | os.PathLike) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ── ffmpeg / ffprobe (subprocess) ───────────────────────────────────────────
def has_ffmpeg() -> bool:
    from shutil import which

    return which("ffmpeg") is not None and which("ffprobe") is not None


def _run(cmd: list[str], quiet: bool = True) -> None:
    get_logger().debug("run: %s", " ".join(cmd))
    subprocess.run(
        cmd,
        check=True,
        stdout=(subprocess.DEVNULL if quiet else None),
        stderr=(subprocess.DEVNULL if quiet else None),
    )


def probe(video: str | os.PathLike) -> dict[str, Any]:
    """ffprobe 로 영상 메타 추출 → {width,height,fps,nb_frames,duration}."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_streams", "-show_format", str(video)],
        check=True, capture_output=True, text=True,
    ).stdout
    info = json.loads(out)
    vstream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})

    def _fps(s: dict) -> float:
        rate = s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1"
        num, _, den = rate.partition("/")
        den_f = float(den) if den else 1.0
        return (float(num) / den_f) if den_f else 0.0

    nb = vstream.get("nb_frames")
    return {
        "width": int(vstream.get("width", 0)),
        "height": int(vstream.get("height", 0)),
        "fps": round(_fps(vstream), 6),
        "nb_frames": int(nb) if nb and str(nb).isdigit() else None,
        "duration": float(info.get("format", {}).get("duration", 0.0) or 0.0),
        "has_audio": any(s.get("codec_type") == "audio" for s in info.get("streams", [])),
    }


def extract_frames(video: str | os.PathLike, out_dir: str | os.PathLike,
                   pattern: str = "%06d.png", start_number: int = 0) -> Path:
    """모든 프레임을 무손실 PNG 시퀀스로 추출. 반환: out_dir."""
    out = ensure_dir(out_dir)
    _run(["ffmpeg", "-y", "-i", str(video), "-start_number", str(start_number),
          "-vsync", "0", str(out / pattern)])
    return out


def extract_audio(video: str | os.PathLike, out_wav: str | os.PathLike) -> Optional[Path]:
    """오디오 트랙 분리(없으면 None)."""
    if not probe(video).get("has_audio"):
        return None
    out = Path(out_wav)
    ensure_dir(out.parent)
    _run(["ffmpeg", "-y", "-i", str(video), "-vn", "-acodec", "pcm_s16le", str(out)])
    return out


def frames_to_video(frames_dir: str | os.PathLike, out: str | os.PathLike, fps: float,
                    codec: str = "ffv1", pattern: str = "%06d.png",
                    pix_fmt: str = "yuv420p", crf: Optional[int] = None,
                    start_number: int = 0) -> Path:
    """프레임 시퀀스 → 영상. ffv1=무손실 중간본, libx264/265/av1=최종."""
    out = Path(out)
    ensure_dir(out.parent)
    cmd = ["ffmpeg", "-y", "-framerate", str(fps), "-start_number", str(start_number),
           "-i", str(Path(frames_dir) / pattern), "-c:v", codec]
    if crf is not None and codec != "ffv1":
        cmd += ["-crf", str(crf)]
    if codec != "ffv1":
        cmd += ["-pix_fmt", pix_fmt, "-movflags", "+faststart"]  # 플레이어 호환(moov 앞으로)
    cmd += [str(out)]
    _run(cmd)
    return out


def mux_audio(video: str | os.PathLike, audio: Optional[str | os.PathLike],
              out: str | os.PathLike) -> Path:
    """영상에 원본 오디오 merge(오디오 None 이면 영상만 복사)."""
    out = Path(out)
    ensure_dir(out.parent)
    if audio is None:
        _run(["ffmpeg", "-y", "-i", str(video), "-c", "copy",
              "-movflags", "+faststart", str(out)])
        return out
    _run(["ffmpeg", "-y", "-i", str(video), "-i", str(audio),
          "-c:v", "copy", "-c:a", "aac", "-shortest", "-map", "0:v:0", "-map", "1:a:0",
          "-movflags", "+faststart", str(out)])
    return out


def mux_dub(video: str | os.PathLike, dub_audio: str | os.PathLike,
            out: str | os.PathLike, bg_volume: float = 0.3,
            voice_volume: float = 1.8,
            bg_audio: Optional[str | os.PathLike] = None,
            loudnorm: bool = True, limiter: bool = False,
            limit: float = 0.95) -> Path:
    """배경 오디오(낮춤=더킹) + 더빙 보이스 믹스 → 영상에 입힘(faststart).

    bg_audio=None: 영상의 원본 오디오를 배경으로 사용.
    bg_audio 지정: 원본 대신 그 트랙(예: 보컬 제거 스템)을 배경으로 → 원본 목소리 제거.
    bg_volume=0 이면 배경 완전 제거(더빙만). ASMR 등은 bg_volume 을 높여 원음 보존.
    loudnorm=True: 최종 라우드니스 정규화(EBU R128, -16 LUFS — 플랫폼 표준에 근접).
    limiter=True(loudnorm=False 일 때): 브릭월 리미터(alimiter)로 피크만 제한 →
        ASMR 다이내믹은 보존하면서 합산 클리핑(째짐) 방지. limit=최대 진폭(0~1).
    """
    out = Path(out)
    ensure_dir(out.parent)
    if bg_audio is not None:
        cmd = ["ffmpeg", "-y", "-i", str(video), "-i", str(bg_audio), "-i", str(dub_audio)]
        pre = (f"[1:a]volume={bg_volume}[bg];[2:a]volume={voice_volume}[voc];"
               f"[bg][voc]amix=inputs=2:duration=first:normalize=0")
    else:
        cmd = ["ffmpeg", "-y", "-i", str(video), "-i", str(dub_audio)]
        pre = (f"[0:a]volume={bg_volume}[bg];[1:a]volume={voice_volume}[voc];"
               f"[bg][voc]amix=inputs=2:duration=first:normalize=0")
    if loudnorm:
        post = "[mix];[mix]loudnorm=I=-16:TP=-1.5:LRA=11[a]"
    elif limiter:
        post = f"[mix];[mix]alimiter=limit={limit}[a]"   # 피크만 제한, 다이내믹 보존
    else:
        post = "[a]"
    filt = pre + post
    cmd += ["-filter_complex", filt, "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", str(out)]
    _run(cmd)
    return out


def burn_subtitles(video: str | os.PathLike, ass_path: str | os.PathLike,
                   out: str | os.PathLike, fonts_dir: Optional[str | os.PathLike] = None,
                   crf: int = 18, pix_fmt: str = "yuv420p") -> Path:
    """ASS 자막을 영상에 번인(원본 영상 위에 덧입힘). 오디오는 그대로 복사, faststart.

    원본 한국어 자막은 화면에 남아 있고, ASS 의 일본어가 그 위/아래에 추가로 렌더된다.
    """
    out = Path(out)
    ensure_dir(out.parent)
    af = f"ass={ass_path}"
    if fonts_dir:
        af += f":fontsdir={fonts_dir}"
    _run(["ffmpeg", "-y", "-i", str(video), "-vf", af,
          "-c:v", "libx264", "-crf", str(crf), "-pix_fmt", pix_fmt,
          "-c:a", "copy", "-movflags", "+faststart", str(out)])
    return out
