"""convert_short — ai-video 본문에 일본어 제목·자막·TTS 를 새로 입힌다 (등급 J, 2026-08-13 v2).

## 분업 (사용자 결정 8/13: "쇼츠 내용만 가지고 와서 제목·자막·TTS 는 일본어로 렌더")

ai-video 는 `--no-subtitles --no-tts-subtitles --no-title-overlay --no-tts-audio` 로
**내용까지만** 만든다: 본문 영상 + 원본 오디오. 텍스트도 KR TTS 오디오도 없다.
대신 run 폴더에 타이밍 원료를 남긴다 — 이 모듈은 그걸 받아 일본어판을 만든다.

  edit_plan.json       → 제목 텍스트(layout.top_title) · 오디오 게인
  subtitles.ass        → 대사 자막 텍스트+타이밍 (원본 오디오 전사)
  tts_subtitles.ass    → 나레이션 텍스트+타이밍 (cue 단위)

## v1(블러 방식)을 버린 이유 — 8/13 실물 실측 세 가지

  ① 제목 밴드 블러가 실물 2줄 제목을 반쯤 놓쳤고, 일본어 제목은 줄바꿈 없이 화면 밖으로
  ② 나레이션 대본 전문이 타이밍 분할 없이 한 덩어리 자막으로 화면을 덮었다
  ③ KR TTS 가 원본 오디오에 이미 믹스되어 있어 일본어로 교체할 방법이 없었다
     (edit_plan 의 use_original_audio 는 나레이션 구간 표시가 아니었다 — narration_spans: 0)

v2 는 지우지 않는다. 애초에 없는 화면에 그리고, 없는 오디오를 얹는다.

## 오디오

원본 오디오는 그대로 두고, 나레이션 cue 구간만 덕킹(기본 0.3배) 후 일본어 TTS 를 얹는다.
TTS 는 ElevenLabs(채널 voice_id 필수 — 잔망루피 클론이 전역 기본값이라 명시 없이는 거부),
cue 길이에 _fit_audio 로 맞춘다(과길이 환각 차단 — dub.py 와 같은 장치).
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
from typing import Any, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from engine.common import ensure_dir, get_logger, load_config, resolve_path  # noqa: E402

log = get_logger("convert_short")

_ASS_TIME = re.compile(r"^(\d+):(\d{2}):(\d{2})[.:](\d{2})$")


# ───────── 순수 (테스트 대상) ─────────
def ass_time_to_sec(t: str) -> float:
    """'0:00:12.34' → 12.34초. ASS 는 센티초 2자리."""
    m = _ASS_TIME.match(t.strip())
    if not m:
        raise ValueError(f"ASS 시각 형식 아님: {t!r}")
    h, mi, s, cs = (int(g) for g in m.groups())
    return h * 3600 + mi * 60 + s + cs / 100.0


def sec_to_ass_time(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600); mi = int(sec % 3600 // 60)
    s = int(sec % 60); cs = int(round(sec % 1 * 100))
    if cs == 100:
        s, cs = s + 1, 0
    return f"{h}:{mi:02d}:{s:02d}.{cs:02d}"


def parse_ass_events(text: str) -> list[dict[str, Any]]:
    """ASS 본문 → [{start, end, text}]. Dialogue 줄만, 태그({\\...})·개행 태그는 걷어낸다.

    ai-video 가 만든 파일이 원료지만 Format 순서를 신뢰하지 않고 헤더에서 읽는다 —
    빌더가 바뀌어도 여기가 조용히 어긋나지 않게.

    Format 은 [Events] 섹션 안의 것만 쓴다 — 실제 산출물은 [V4+ Styles] 에도
    Format: 이 있어서(16필드), 그걸 집으면 Dialogue(10필드)가 전부 len 미달로
    버려진다. 8/13 실측: 혜미리예채파 ep2 J 변환이 자막 0건·나레이션 0건."""
    fmt = None
    section = ""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section != "events":
                fmt = None
        elif line.lower().startswith("format:") and section in ("events", ""):
            fmt = [f.strip().lower() for f in line.split(":", 1)[1].split(",")]
        elif line.lower().startswith("dialogue:"):
            if fmt is None:
                fmt = ["layer", "start", "end", "style", "name",
                       "marginl", "marginr", "marginv", "effect", "text"]
            vals = line.split(":", 1)[1].split(",", len(fmt) - 1)
            if len(vals) < len(fmt):
                continue
            row = dict(zip(fmt, vals))
            raw = row.get("text", "")
            clean = re.sub(r"\{[^}]*\}", "", raw).replace("\\N", " ").replace("\\n", " ").strip()
            if not clean:
                continue
            out.append({"start": ass_time_to_sec(row["start"]),
                        "end": ass_time_to_sec(row["end"]), "text": clean})
    out.sort(key=lambda e: e["start"])
    return out


def wrap_jp(text: str, max_chars: int = 14, max_lines: int = 3) -> str:
    """일본어 줄바꿈(문자 수 기준 — CJK 는 단어 경계가 없다). 넘치면 뒷줄 말줄임.
    8/13 실측 ①의 재발 방지: 제목이 한 줄로 화면 밖까지 뻗었다."""
    t = (text or "").strip()
    lines = []
    while t and len(lines) < max_lines:
        lines.append(t[:max_chars])
        t = t[max_chars:]
    if t:
        lines[-1] = lines[-1][:-1] + "…"
    return "\\N".join(lines)


def build_ja_ass(title: Optional[str], dlg_events: list[dict[str, Any]],
                 nar_events: list[dict[str, Any]], w: int, h: int,
                 duration: float, font: str = "Noto Sans CJK JP",
                 title_max_chars: int = 14, sub_max_chars: int = 16) -> str:
    """일본어 ASS 한 장: 제목(상단 밴드 고정) + 대사 자막(하단) + 나레이션 자막(중하단).

    위치는 ai-video 기본 레이아웃(상단 밴드 ~톱 21%, 하단 라벨 밴드)과 맞춘다:
      제목: an8, 상단 밴드 안(MarginV = h*0.045)
      나레이션: an2, 본문 하단(MarginV = h*0.24) — 하단 라벨 밴드 위
      대사: an2, 맨 아래(MarginV = h*0.13)"""
    def ev(style, start, end, text):
        return f"Dialogue: 0,{sec_to_ass_time(start)},{sec_to_ass_time(end)},{style},,0,0,0,,{text}"

    ts = int(h * 0.036); ss = int(h * 0.026); ns = int(h * 0.028)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, BorderStyle
Style: JTitle,{font},{ts},&H00FFFFFF,&H00000000,&H96000000,1,3,1,8,40,40,{int(h*0.045)},1
Style: JNarr,{font},{ns},&H00FFFFFF,&H00000000,&H00000000,1,3,1,2,40,40,{int(h*0.24)},1
Style: JDlg,{font},{ss},&H0000FFFF,&H00000000,&H00000000,1,3,1,2,40,40,{int(h*0.13)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if (title or "").strip():
        lines.append(ev("JTitle", 0, duration, wrap_jp(title, title_max_chars, 3)))
    for e in nar_events:
        lines.append(ev("JNarr", e["start"], e["end"], wrap_jp(e["text"], sub_max_chars, 2)))
    for e in dlg_events:
        lines.append(ev("JDlg", e["start"], e["end"], wrap_jp(e["text"], sub_max_chars, 2)))
    return head + "\n".join(lines) + "\n"


def duck_expr(spans: list[tuple[float, float]], gain: float = 0.3) -> str:
    """나레이션 구간만 원본 오디오를 gain 배로 — 밖에서는 1.0(원본 그대로).
    volume enable 식. 순수 — 테스트 대상."""
    if not spans:
        return ""
    cond = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in spans)
    return f"volume={gain}:enable='{cond}'"


def audio_graph(n_tts: int, spans: list[tuple[float, float]], gain: float = 0.3) -> str:
    """[0:a](원본) + [1..n](TTS 파일) → [aout]. adelay 로 cue 시작에 배치, amix.
    순수 — 테스트 대상."""
    duck = duck_expr(spans, gain)
    parts = [f"[0:a]{duck or 'anull'}[bed]"]
    ins = "[bed]"
    if n_tts:
        for i, (s, _e) in enumerate(zip([sp[0] for sp in spans], range(n_tts))):
            ms = int(spans[i][0] * 1000)
            parts.append(f"[{i+1}:a]adelay={ms}|{ms}[tts{i}]")
        ins = "[bed]" + "".join(f"[tts{i}]" for i in range(n_tts))
        parts.append(f"{ins}amix=inputs={n_tts+1}:duration=first:normalize=0[aout]")
    else:
        parts.append("[bed]anull[aout]")
    return ";".join(parts)


# ───────── 실행부 ─────────
def _probe_duration(path: str) -> float:
    r = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def convert(video: str, plan_path: str, out_path: str, config: dict[str, Any],
            voice_id: Optional[str] = None, no_dub: bool = False,
            subs_path: Optional[str] = None, tts_subs_path: Optional[str] = None) -> dict[str, Any]:
    from engine.translate import transcreate

    plan = json.loads(pathlib.Path(plan_path).read_text(encoding="utf-8"))
    # 제목의 개행은 번역 전에 한 줄로 편다(8/13 실측 ⑤): LLM 이 source 를 개행 없이
    # 되돌려주면 transcreate 의 by_source 매칭이 빗나가 target="" 이 되고, 아래
    # 폴백이 한국어 제목을 그대로 태운다. 일본어 줄바꿈은 wrap_jp 가 다시 잡는다.
    title = re.sub(r"\s*\n\s*", " ", ((plan.get("layout") or {}).get("top_title") or "")).strip()
    raw = (plan.get("layout") or {}).get("canvas") or "1080x1920"
    w, h = (int(x) for x in raw.lower().split("x"))
    duration = _probe_duration(video)

    dlg = parse_ass_events(pathlib.Path(subs_path).read_text(encoding="utf-8")) \
        if subs_path and pathlib.Path(subs_path).exists() else []
    nar = parse_ass_events(pathlib.Path(tts_subs_path).read_text(encoding="utf-8")) \
        if tts_subs_path and pathlib.Path(tts_subs_path).exists() else []

    # 번역 — 제목 + 대사 + 나레이션을 한 번에(배치·용어집은 transcreate 가 처리)
    texts = ([title] if title else []) + [e["text"] for e in dlg] + [e["text"] for e in nar]
    ja_map: dict[str, str] = {}
    fallback = 0
    if texts:
        ents = transcreate(texts, config)
        # transcreate 는 입력 순서·개수를 보존한다(항목별 append) — source 문자열
        # 재대조가 아니라 인덱스로 잇는다. target 이 비면(LLM 이 source 를 변형해
        # 되돌려 by_source 가 빗나간 경우 등) 소리 내고 원문 폴백을 센다.
        for i, t in enumerate(texts):
            tgt = (ents[i].target or "").strip() if i < len(ents) else ""
            if not tgt:
                fallback += 1
                log.warning("번역 누락 폴백: %r", t[:40])
            ja_map[t] = tgt or t
    ja_title = ja_map.get(title, title) if title else None
    if ja_title and re.search(r"[가-힣]", ja_title):
        # 제목이 한국어인 채 나가면 채널 정체성이 무너진다(8/13 실측 ⑤ — 사용자
        # 스크린샷). 조용한 폴백 대신 잡을 죽여 재시도로 보낸다.
        raise RuntimeError(f"제목 번역 실패 — 한글이 남아 있음: {ja_title[:60]!r}")
    for e in dlg + nar:
        e["text"] = ja_map.get(e["text"], e["text"])
    log.info("번역 %d건 (제목 %s · 대사 %d · 나레이션 %d · 폴백 %d)",
             len(texts), "O" if title else "X", len(dlg), len(nar), fallback)

    work = ensure_dir(pathlib.Path(out_path).parent)
    rcfg = config.get("render", {})
    ass_path = work / "ja_convert.ass"
    ass_path.write_text(
        build_ja_ass(ja_title, dlg, nar, w, h, duration,
                     sub_max_chars=int(rcfg.get("line_max_chars", 16))),
        encoding="utf-8")

    # ① 영상: 본문에 일본어 ASS 번인(원본 화면 무변경 — 지울 것이 없다)
    fonts = config.get("paths", {}).get("fonts_dir")
    burned = work / "ja_burned.mp4"
    vf = f"ass={ass_path}" + (f":fontsdir={resolve_path(fonts)}" if fonts else "")
    r = subprocess.run(["ffmpeg", "-y", "-i", video, "-vf", vf,
                        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                        "-c:a", "copy", "-movflags", "+faststart", str(burned)],
                       capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"자막 번인 실패: {(r.stderr or '')[-300:]}")

    # ② 오디오: 나레이션 cue 에 일본어 TTS + 원본 덕킹. cue 가 없거나 --no-dub 면 그대로.
    spans = [(e["start"], min(e["end"], duration or e["end"])) for e in nar]
    if no_dub or not spans:
        pathlib.Path(out_path).unlink(missing_ok=True)
        pathlib.Path(burned).rename(out_path)
        return {"final": str(out_path), "narration_cues": len(spans), "dub": False,
                "subs": len(dlg), "tr_fallback": fallback}

    from src.dub import _fit_audio, synthesize_segment
    seg_files = []
    for i, e in enumerate(nar):
        raw_b = synthesize_segment(e["text"], config, voice_id=voice_id)
        if not raw_b:
            raise RuntimeError(f"TTS 합성 실패(cue {i}): 빈 결과")
        seg = work / f"ja_tts_{i}.bin"
        seg.write_bytes(raw_b)
        fit = work / f"ja_tts_{i}_fit.wav"
        _fit_audio(seg, fit, target_sec=max(0.5, e["end"] - e["start"]),
                   max_len=max(0.6, e["end"] - e["start"]))
        seg_files.append(str(fit))

    gain = float(config.get("jp_convert", {}).get("duck_volume", 0.3))
    graph = audio_graph(len(seg_files), spans, gain)
    cmd = ["ffmpeg", "-y", "-i", str(burned)]
    for f in seg_files:
        cmd += ["-i", f]
    cmd += ["-filter_complex", graph, "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(out_path)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"오디오 합성 실패: {(r.stderr or '')[-300:]}")
    pathlib.Path(burned).unlink(missing_ok=True)
    return {"final": str(out_path), "narration_cues": len(spans), "dub": True,
            "subs": len(dlg), "tr_fallback": fallback, "tts_segments": len(seg_files)}


def _parse_args(argv=None):
    import argparse
    p = argparse.ArgumentParser(description="ai-video 본문 → 일본어 제목·자막·TTS (등급 J)")
    p.add_argument("--video", required=True, help="본문 영상(텍스트·KR TTS 없음)")
    p.add_argument("--edit-plan", required=True)
    p.add_argument("--subs", default=None, help="subtitles.ass (대사 자막 원료)")
    p.add_argument("--tts-subs", default=None, help="tts_subtitles.ass (나레이션 원료)")
    p.add_argument("--out", required=True)
    p.add_argument("--voice", default=None, help="ElevenLabs voice_id")
    p.add_argument("--no-dub", action="store_true", help="자막·제목만(나레이션 TTS 생략)")
    p.add_argument("--config", default=None)
    return p.parse_args(argv)


if __name__ == "__main__":
    a = _parse_args()
    res = convert(a.video, a.edit_plan, a.out, load_config(a.config),
                  voice_id=a.voice, no_dub=a.no_dub,
                  subs_path=a.subs, tts_subs_path=a.tts_subs)
    print(res)
