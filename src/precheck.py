"""레벨 실측 판별(pre-check) — 처리 전에 영상을 열어 무엇이 있는지 '실측'한다.

제목 추정(LLM level_guess)의 한계 보완: 번인 자막 없는 Short 에 Level B 를 돌리면
OCR 노이즈 오검출로 영상이 망가진다(2026-07-01 loopy_short 사례). 그래서:

  프레임 샘플 OCR → 번인 한국어 자막 실측  +  오디오 ASR → 대사 유무 실측
  → 라우팅: 번인 있음=B(캡션 교체) / 번인 없음+대사 있음=C(더빙 플로우)
            / 둘 다 없음=A(영상 무변환, 메타데이터만)

무거운 의존(paddleocr·faster-whisper)은 lazy import — 판정 규칙은 순수 함수로 분리.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from typing import Any  # noqa: E402

from engine.common import get_logger, write_json, resolve_path, ensure_dir  # noqa: E402

log = get_logger("precheck")

_HANGUL_RE = re.compile(r"[가-힣]")


# ── 순수: 판정 규칙 ───────────────────────────────────────────────────────
def hangul_chars(text: str) -> int:
    return len(_HANGUL_RE.findall(text or ""))


def solid_hit_frames(frames: list[dict[str, Any]], min_conf: float, min_hangul: int) -> int:
    """'진짜 번인 텍스트'로 보이는 검출이 있는 프레임 수.

    노이즈 필터: 신뢰도 min_conf 이상 AND 한글 min_hangul 자 이상인 region 만 인정."""
    hits = 0
    for f in frames:
        if any(float(r.get("confidence", 0)) >= min_conf
               and hangul_chars(r.get("text", "")) >= min_hangul
               for r in f.get("regions", [])):
            hits += 1
    return hits


def decide_route(burn_frames: int, dialogue_segs: int, min_persist: int) -> str:
    """실측 → 레벨. 번인(지속 프레임 min_persist 이상)=B, 대사만=C(더빙), 없으면 A."""
    if burn_frames >= min_persist:
        return "B"
    if dialogue_segs >= 1:
        return "C"
    return "A"


def ensure_korean_capable(actual_backend: str, requested: str) -> None:
    """OCR 폴백 가드 — 한국어 못 읽는 백엔드로 조용히 폴백되면 번인 판정이
    항상 0 이 되어 라우트가 뒤집힌다(B→C/A). 판정을 계속하느니 실패가 낫다."""
    if actual_backend == requested:
        return
    if actual_backend == "rapidocr":   # 기본 모델 한국어 인식 불가(engine/detect 경고와 동일 근거)
        raise RuntimeError(
            f"OCR 백엔드 '{requested}' 초기화 실패 → '{actual_backend}' 폴백은 한국어 인식 불가 — "
            "번인 판정 불가능이라 precheck 를 중단합니다. paddleocr 설치/모델 확인 필요.")


# ── 실측 (lazy 의존) ─────────────────────────────────────────────────────
def _sample_frames(video: str, n: int, tmp: pathlib.Path) -> list[pathlib.Path]:
    """영상에서 n 장 균등 샘플 추출(ffmpeg)."""
    from engine.common import probe
    dur = float(probe(video).get("duration", 0.0) or 0.0)
    outs = []
    for i in range(n):
        t = dur * (i + 0.5) / n if dur > 0 else 0
        fp = tmp / f"pc_{i:03d}.png"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", video,
                        "-frames:v", "1", str(fp)], check=True)
        if fp.exists():
            outs.append(fp)
    return outs


def _ocr_probe(video: str, config: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """샘플 프레임 OCR → (프레임 dict 목록, 실사용 백엔드명).

    요청 백엔드가 한국어 불가 백엔드로 조용히 폴백되면 중단(ensure_korean_capable)."""
    import cv2
    from engine.detect import make_ocr, _ocr_scaled

    pc = config.get("autopilot", {}).get("precheck", {})
    n = int(pc.get("frames", 8))
    dcfg = config.get("detect", {})
    requested = dcfg.get("ocr_backend", "paddleocr")
    ocr = make_ocr(requested, dcfg.get("languages", ["korean", "en"]),
                   paddle_opts={"det_model": dcfg.get("paddle_det_model"),
                                "rec_model": dcfg.get("paddle_rec_model")})
    ensure_korean_capable(ocr.name, requested)
    down = int(dcfg.get("ocr_downscale_width", 0))
    frames = []
    with tempfile.TemporaryDirectory() as td:
        for i, fp in enumerate(_sample_frames(video, n, pathlib.Path(td))):
            img = cv2.imread(str(fp))
            if img is None:
                continue
            regions = [{"text": t, "confidence": float(c)}
                       for (_, t, c) in _ocr_scaled(ocr, img, down)]
            frames.append({"frame_idx": i, "regions": regions})
    return frames, ocr.name


def _asr_probe(video: str, config: dict[str, Any]) -> int:
    """한국어 대사 세그먼트 수(한글 2자 이상 + 할루시네이션 필터).

    판정과 실행의 기준 일치: 모델·필터를 실제 더빙 플로우(src/dub.transcribe)와 공유 —
    여기서 대사로 세면 더빙도 그 대사를 쓴다."""
    from src.dub import transcribe

    segs = transcribe(video, config, language="ko")
    return sum(1 for s in segs if hangul_chars(s["text"]) >= 2)


def precheck(video: str, video_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """실측 판별 실행 → outputs/{id}/precheck.json + 결과 dict(route, 근거)."""
    pc = config.get("autopilot", {}).get("precheck", {})
    min_conf = float(pc.get("min_conf", 0.75))
    min_hangul = int(pc.get("min_hangul", 2))
    min_persist = int(pc.get("min_persist", 2))

    frames, backend = _ocr_probe(video, config)
    burn = solid_hit_frames(frames, min_conf, min_hangul)
    dialogue = _asr_probe(video, config)
    route = decide_route(burn, dialogue, min_persist)

    result = {"video_id": video_id, "route": route,
              "burn_frames": burn, "dialogue_segs": dialogue,
              "sampled_frames": len(frames), "ocr_backend": backend,
              "params": {"min_conf": min_conf, "min_hangul": min_hangul,
                         "min_persist": min_persist},
              "ocr_frames": frames}
    base = ensure_dir(resolve_path(f"{config['paths']['outputs_dir']}/{video_id}"))
    write_json(result, base / "precheck.json")
    log.info("precheck(%s): route=%s (번인 %d프레임, 대사 %d세그)",
             video_id, route, burn, dialogue)
    return result
