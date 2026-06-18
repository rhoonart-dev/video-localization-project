"""선별 더빙 (C-9) — Level C 한정. 일본어 자막 → TTS 초안.

ja 자막(ja.srt/ja.ass)을 입력으로 ElevenLabs 등 TTS 호출, persona 보이스 디렉션 반영.
한국 성우 클로닝이 아니라 "일본 캐릭터 보이스 디렉션" 기본.
출력: outputs/{video_id}/dub_ja_draft.wav + alignment_report.json.
⚠ retention 리스크·hero 영상은 사람/성우 검토. 자동 영상 합성·게시 금지(드래프트 오디오까지만).

[Level C 가드] level != C 면 거부. 자막 파싱·정렬 리포트는 순수, TTS/ffmpeg 만 외부.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pathlib import Path  # noqa: E402
from typing import Any, Optional  # noqa: E402

from engine import common  # noqa: E402
from engine.common import ensure_dir, get_logger, get_secret, load_config, resolve_path, write_json  # noqa: E402

log = get_logger("dub")


# ── 순수 헬퍼 ─────────────────────────────────────────────────────────────
def require_level_c(level: str) -> None:
    if level != "C":
        raise ValueError(f"더빙은 Level C 한정. 현재 level={level}. 게이트/등급 확인.")


def _srt_time(t: str) -> float:
    h, m, rest = t.split(":")
    s, _, ms = rest.replace(".", ",").partition(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + (int(ms) / 1000 if ms else 0.0)


def _ass_time(t: str) -> float:
    h, m, rest = t.split(":")
    s, _, cs = rest.partition(".")
    return int(h) * 3600 + int(m) * 60 + int(s) + (int(cs) / 100 if cs else 0.0)


def parse_segments(subtitle_path: str) -> list[dict[str, Any]]:
    """ja.srt 또는 ja.ass → [{start,end,text}] (초)."""
    text = Path(subtitle_path).read_text(encoding="utf-8")
    segs: list[dict[str, Any]] = []
    if subtitle_path.endswith(".ass"):
        for line in text.splitlines():
            if not line.startswith("Dialogue:"):
                continue
            fields = line[len("Dialogue:"):].split(",", 9)
            if len(fields) < 10:
                continue
            body = re.sub(r"\{[^}]*\}", "", fields[9]).replace("\\N", " ").strip()
            if body:
                segs.append({"start": _ass_time(fields[1].strip()),
                             "end": _ass_time(fields[2].strip()), "text": body})
    else:  # SRT
        for block in re.split(r"\n\s*\n", text.strip()):
            lines = [ln for ln in block.splitlines() if ln.strip()]
            if len(lines) < 2:
                continue
            tl = next((ln for ln in lines if "-->" in ln), None)
            if not tl:
                continue
            start, _, end = tl.partition("-->")
            body = " ".join(lines[lines.index(tl) + 1:]).strip()
            if body:
                segs.append({"start": _srt_time(start.strip()),
                             "end": _srt_time(end.strip()), "text": body})
    segs.sort(key=lambda s: s["start"])
    return segs


def build_alignment_report(video_id: str, segments: list[dict[str, Any]],
                           voice_id: str) -> dict[str, Any]:
    return {
        "_warning": "더빙 초안. retention 리스크·hero 는 사람/성우 검토. 자동 게시 금지.",
        "video_id": video_id,
        "voice_id": voice_id,
        "segment_count": len(segments),
        "total_speech_sec": round(sum(s["end"] - s["start"] for s in segments), 2),
        "segments": segments,
    }


# ── TTS (ElevenLabs, lazy) ───────────────────────────────────────────────
def synthesize_segment(text: str, voice_id: str, config: dict[str, Any]) -> bytes:
    key = get_secret("ELEVENLABS_API_KEY", required=True)
    try:
        from elevenlabs.client import ElevenLabs
    except ImportError as e:
        raise ImportError("elevenlabs 필요: pip install elevenlabs") from e
    client = ElevenLabs(api_key=key)
    audio = client.text_to_speech.convert(
        voice_id=voice_id, text=text,
        model_id=config.get("dub", {}).get("tts_model", "eleven_multilingual_v2"),
        output_format="mp3_44100_128")
    return b"".join(audio) if hasattr(audio, "__iter__") else audio


def dub(video_id: str, subtitle_path: str, level: str, config: dict[str, Any],
        voice_id: Optional[str] = None) -> dict[str, Any]:
    require_level_c(level)
    voice_id = voice_id or config.get("dub", {}).get("voice_id", "")
    if not voice_id:
        raise ValueError("voice_id 필요(일본 캐릭터 보이스 디렉션). --voice 또는 config.dub.voice_id")

    segments = parse_segments(subtitle_path)
    base = ensure_dir(resolve_path(f"{config['paths']['outputs_dir']}/{video_id}"))
    seg_dir = ensure_dir(base / "dub_segments")
    log.warning("Level C 더빙 초안 생성. hero/리텐션 리스크는 사람 검토 필수.")

    seg_files: list[tuple[float, Path]] = []
    for i, seg in enumerate(segments):
        data = synthesize_segment(seg["text"], voice_id, config)
        fp = seg_dir / f"seg_{i:04d}.mp3"
        fp.write_bytes(data)
        seg_files.append((seg["start"], fp))

    draft = base / "dub_ja_draft.wav"
    _assemble_timeline(seg_files, draft)
    write_json(build_alignment_report(video_id, segments, voice_id),
               base / "alignment_report.json")
    log.info("더빙 초안(검토 전): %s (세그먼트 %d)", draft, len(segments))
    return {"draft": str(draft), "segments": len(segments)}


def _assemble_timeline(seg_files: list[tuple[float, Path]], out: Path) -> None:
    """각 세그먼트를 시작 시각에 배치해 한 트랙으로 mix(ffmpeg adelay+amix)."""
    if not seg_files:
        log.warning("세그먼트 없음 → 더빙 트랙 생략")
        return
    if not common.has_ffmpeg():
        raise RuntimeError("ffmpeg 필요(더빙 트랙 합성).")
    import subprocess

    cmd: list[str] = ["ffmpeg", "-y"]
    for _, fp in seg_files:
        cmd += ["-i", str(fp)]
    parts, labels = [], []
    for idx, (start, _) in enumerate(seg_files):
        ms = int(start * 1000)
        parts.append(f"[{idx}]adelay={ms}|{ms}[a{idx}]")
        labels.append(f"[a{idx}]")
    filt = ";".join(parts) + ";" + "".join(labels) + \
        f"amix=inputs={len(seg_files)}:normalize=0[out]"
    cmd += ["-filter_complex", filt, "-map", "[out]", str(out)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Level C 더빙 초안(드래프트 오디오까지)")
    p.add_argument("--video-id", required=True)
    p.add_argument("--subtitle", required=True, help="ja.srt 또는 ja.ass")
    p.add_argument("--level", default="C", help="C 가 아니면 거부")
    p.add_argument("--voice", default=None, help="ElevenLabs voice_id")
    p.add_argument("--config", default=None)
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    dub(args.video_id, args.subtitle, args.level, load_config(args.config), voice_id=args.voice)


if __name__ == "__main__":
    main()
