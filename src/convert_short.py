"""ai-video 쇼츠 일본어 변환 (ショトコン, 2026-08-13 사용자 결정) — ai-video 무변경.

번역은 이 프로젝트 담당이다(사용자 결정). ai-video 가 만든 한국어 쇼츠(shorts.mp4)와
run 산출물 edit_plan.json 을 받아 일본어판을 만든다:

  · 영상 안 제목(layout.top_title)  → 일본어로 교체
  · 대사 자막(timeline[].subtitle)  → 일본어로 교체 (원본 방송 오디오는 그대로 — 자막으로만)
  · TTS 나레이션 구간(use_original_audio=false) → 한국어 보컬만 걷어내고 일본어 TTS 로 교체
  · 노래·배경음·리액션 → 원본 유지

OCR 이 필요 없다 — 텍스트·타이밍·믹스 게인이 전부 edit_plan 에 있다(ai-video pipeline.py 가
run 마다 기록). 원 텍스트가 놓인 밴드는 블러 배경 위라, 강한 재블러로 지우고 그 위에
일본어를 얹는다(LaMa 인페인트 불필요).

⚠ 좌표 규약: edit_plan 의 clip_start/end_sec 은 '원본 방송' 좌표다. 출력 쇼츠 좌표는
  클립을 순서대로 이어붙인 누적합으로 계산한다(output_timeline) — ai-video 몽타주 규약.
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from typing import Any, Optional  # noqa: E402

from engine.common import ensure_dir, get_logger, load_config, read_json, resolve_path  # noqa: E402
from engine.render import build_ass  # noqa: E402

log = get_logger("convert_short")


# ───────── 순수 (테스트 대상) ─────────
def output_timeline(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """edit_plan.timeline(원본 좌표) → 출력 쇼츠 좌표의 클립 목록.

    [{start, end, subtitle, narration}] — start/end 는 출력 영상 기준(누적합).
    narration = use_original_audio 가 false 인 구간(ai-video TTS 나레이션이 얹힌 곳)."""
    out, t = [], 0.0
    for c in plan.get("timeline") or []:
        dur = max(0.0, float(c.get("clip_end_sec", 0)) - float(c.get("clip_start_sec", 0)))
        if dur <= 0:
            continue
        out.append({"start": round(t, 3), "end": round(t + dur, 3),
                    "subtitle": (c.get("subtitle") or "").strip(),
                    "narration": not c.get("use_original_audio", True)})
        t += dur
    return out


def collect_texts(plan: dict[str, Any], timeline: list[dict[str, Any]]) -> list[str]:
    """번역할 원문 목록(중복 제거, 순서 유지): 제목 + 클립 자막."""
    seen, out = set(), []
    title = ((plan.get("layout") or {}).get("top_title") or "").strip()
    for t in [title] + [c["subtitle"] for c in timeline]:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def narration_spans(timeline: list[dict[str, Any]],
                    min_gap: float = 0.25) -> list[tuple[float, float]]:
    """나레이션 구간(출력 좌표) — 인접 구간은 합친다(오디오 필터 표현식을 짧게)."""
    spans: list[tuple[float, float]] = []
    for c in timeline:
        if not c["narration"]:
            continue
        if spans and c["start"] - spans[-1][1] <= min_gap:
            spans[-1] = (spans[-1][0], c["end"])
        else:
            spans.append((c["start"], c["end"]))
    return spans


def canvas_size(plan: dict[str, Any], default: tuple[int, int] = (1080, 1920)) -> tuple[int, int]:
    """layout.canvas '1080x1920' → (w, h). 못 읽으면 쇼츠 표준."""
    raw = str((plan.get("layout") or {}).get("canvas") or "")
    try:
        w, h = raw.lower().split("x")
        return int(w), int(h)
    except ValueError:
        return default


def title_band(w: int, h: int) -> tuple[int, int, int, int]:
    """제목 밴드 (x, y, w, h) — 쇼츠 레이아웃 상단 블러 밴드. 채널 디자인 좌표를 따로
    받기 전의 기본값: 상단 4%~16%. v1 실측 후 조정한다."""
    return 0, int(h * 0.04), w, int(h * 0.12)


def subtitle_band(w: int, h: int) -> tuple[int, int, int, int]:
    """자막 밴드 — 하단 72%~88% (ai-video 자막·TTS 자막이 놓이는 영역)."""
    return 0, int(h * 0.72), w, int(h * 0.16)


def blur_filter(boxes: list[tuple[int, int, int, int]], strength: int = 24) -> str:
    """밴드들을 강블러로 지우는 ffmpeg filter_complex. 텍스트가 블러 배경 위라
    강한 재블러만으로 읽을 수 없게 뭉개진다 — 인페인트 대체.
    입력 [0:v] → 출력 [v]. 순수 — 테스트 대상."""
    if not boxes:
        return "[0:v]null[v]"
    parts, cur = [], "0:v"
    for i, (x, y, bw, bh) in enumerate(boxes):
        nxt = "v" if i == len(boxes) - 1 else f"s{i}"
        parts.append(f"[{cur}]split[a{i}][b{i}]")
        parts.append(f"[b{i}]crop={bw}:{bh}:{x}:{y},boxblur={strength}:2[c{i}]")
        parts.append(f"[a{i}][c{i}]overlay={x}:{y}[{nxt}]")
        cur = nxt
    return ";".join(parts)


def ja_events(timeline: list[dict[str, Any]], ja: dict[str, str],
              title: str, total: float) -> list[dict[str, Any]]:
    """일본어 ASS 이벤트: 제목(상단, 전체 길이) + 클립 자막(하단). 순수 — 테스트 대상.
    번역이 빈 항목은 원문 유지(빈 자막을 태우지 않는다 — flagged 는 검수에서 본다)."""
    ev = []
    if title:
        ev.append({"start": 0.0, "end": round(total, 3),
                   "text": ja.get(title) or title, "position": "top-center"})
    for c in timeline:
        if c["subtitle"]:
            ev.append({"start": c["start"], "end": c["end"],
                       "text": ja.get(c["subtitle"]) or c["subtitle"],
                       "position": "bottom-center"})
    return ev


def mute_expr(spans: list[tuple[float, float]]) -> str:
    """ffmpeg volume enable 식(구간 안=참). dub._mute_spans 와 같은 규약. 순수."""
    return "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in spans)


# ───────── 실행부 ─────────
def convert(video: str, plan_path: str, out_path: str, config: dict[str, Any],
            voice_id: Optional[str] = None, no_dub: bool = False,
            blur_subs: bool = False) -> dict[str, Any]:
    plan = read_json(plan_path)
    timeline = output_timeline(plan)
    if not timeline:
        raise SystemExit(f"edit_plan.timeline 비어 있음: {plan_path}")
    w, h = canvas_size(plan)
    title = ((plan.get("layout") or {}).get("top_title") or "").strip()
    total = timeline[-1]["end"]
    work = ensure_dir(pathlib.Path(out_path).parent)

    # [1] 번역 — 이 프로젝트의 트랜스크리에이션(페르소나·용어집·배치)을 그대로 쓴다
    from engine.translate import transcreate
    texts = collect_texts(plan, timeline)
    entries = transcreate(texts, config)
    ja = {e.source: e.target for e in entries if e.target}
    log.info("번역 %d/%d건 (빈 결과는 원문 유지·flagged)", len(ja), len(texts))

    # [2] 화면 — 원 텍스트 밴드 재블러 + 일본어 ASS 번인 (원본 오디오는 이 단계에서 유지)
    ass = work / "ja_convert.ass"
    ass.write_text(build_ass(ja_events(timeline, ja, title, total), w, h,
                             line_max_chars=int(config.get("render", {})
                                                .get("line_max_chars", 16))),
                   encoding="utf-8")
    fonts = resolve_path(config.get("paths", {}).get("fonts_dir", "fonts"))
    # 새 흐름(8/13): generate 가 --no-subtitles --no-tts-subtitles 로 돌아 자막이 애초에
    # 없다 — 제목 밴드만 지우면 된다(영상 본문 위를 블러할 일이 없다). blur_subs 는
    # 자막이 이미 구워진 구 run 호환용.
    boxes = [title_band(w, h)] + ([subtitle_band(w, h)] if blur_subs else [])
    vf = blur_filter(boxes) + f";[v]ass={ass}:fontsdir={fonts}[vo]"
    video_ja = work / "video_ja.mp4"
    subprocess.run(["ffmpeg", "-y", "-i", str(video), "-filter_complex", vf,
                    "-map", "[vo]", "-map", "0:a?", "-c:v", "libx264", "-crf", "18",
                    "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
                    str(video_ja)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # [3] 오디오 — 나레이션 구간만 교체. 대사·노래·배경은 원본 그대로.
    spans = narration_spans(timeline)
    if no_dub or not spans:
        pathlib.Path(video_ja).replace(out_path)
        return {"final": str(out_path), "narration_spans": len(spans), "dub": False}
    if not voice_id:
        raise SystemExit("나레이션 교체에 voice_id 필요 (--voice). "
                         "--no-dub 이면 자막·제목만 바꾼다")

    from src.dub import _fit_audio, _mute_windows, separate_vocals, synthesize_segment
    from engine import common as _common
    audio = _common.extract_audio(str(video), work / "src_audio.wav")
    base_a = work / "audio_keep.wav"
    _mute_windows(audio, base_a, spans)                       # 나레이션 구간만 0 — 나머지 원본
    novoc = separate_vocals(str(video), work / "stems", config)   # 반주 스템(나레이션 밑그림)
    bed = work / "audio_bed.wav"
    inv = [(0.0, spans[0][0])] + [(spans[i][1], spans[i + 1][0]) for i in range(len(spans) - 1)] \
        + [(spans[-1][1], total + 1)]
    _mute_windows(novoc, bed, [(s, e) for s, e in inv if e - s > 0.01])  # 구간 밖은 0

    seg_files = []
    for i, c in enumerate(t for t in timeline if t["narration"] and t["subtitle"]):
        txt = ja.get(c["subtitle"]) or c["subtitle"]
        raw = work / f"tts_{i}.bin"
        raw.write_bytes(synthesize_segment(txt, config, voice_id=voice_id, lang="ja"))
        fit = work / f"tts_{i}.wav"
        _fit_audio(raw, fit, c["end"] - c["start"], max_len=c["end"] - c["start"])
        seg_files.append((fit, c["start"]))
    inputs, fc = ["-i", str(base_a), "-i", str(bed)], []
    for i, (f, at) in enumerate(seg_files):
        inputs += ["-i", str(f)]
        fc.append(f"[{i + 2}:a]adelay={int(at * 1000)}|{int(at * 1000)}[d{i}]")
    labels = "[0:a][1:a]" + "".join(f"[d{i}]" for i in range(len(seg_files)))
    fc.append(f"{labels}amix=inputs={2 + len(seg_files)}:duration=first:normalize=0[am]")
    mixed = work / "audio_ja.wav"
    subprocess.run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(fc),
                    "-map", "[am]", str(mixed)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["ffmpeg", "-y", "-i", str(video_ja), "-i", str(mixed),
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
                    "-b:a", "192k", "-movflags", "+faststart", str(out_path)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"final": str(out_path), "narration_spans": len(spans), "dub": True,
            "tts_segments": len(seg_files)}


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="ai-video 쇼츠 → 일본어판 (제목·자막·나레이션)")
    p.add_argument("--video", required=True)
    p.add_argument("--edit-plan", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--voice", default=None, help="나레이션 ElevenLabs voice_id")
    p.add_argument("--no-dub", action="store_true", help="자막·제목만(나레이션 교체 생략)")
    p.add_argument("--blur-subs", action="store_true",
                   help="하단 자막 밴드도 재블러(자막이 구워진 구 run 호환)")
    p.add_argument("--config", default=None)
    return p.parse_args(argv)


if __name__ == "__main__":
    a = _parse_args()
    res = convert(a.video, a.edit_plan, a.out, load_config(a.config),
                  voice_id=a.voice, no_dub=a.no_dub, blur_subs=a.blur_subs)
    print(res)
