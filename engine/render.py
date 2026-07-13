"""[엔진③-b] 일본어 텍스트 원본 스타일 재합성 (GhostCut 차별 레이어의 나머지 절반).

모드 A(replace): 인페인팅된 배경 위에 일본어를 원본 위치·색·크기로 Pillow 합성.
                 한국어→일본어 폰트는 font_map.yaml 로 매핑.
모드 B(subtitle): ASS(libass)/SRT 자막 트랙 생성(스타일 지정).

폰트 해석·텍스트 줄바꿈·ASS 타임코드·ASS/SRT 빌드는 순수 → 테스트 가능.
프레임 합성(Pillow)만 PIL 사용.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Optional

from engine.common import ensure_dir, get_logger, load_yaml, resolve_path
from engine.schemas import BBox, DetectionDoc, Style, TranslationDoc

log = get_logger("render")


# ── 순수 헬퍼 ─────────────────────────────────────────────────────────────
def resolve_font(style: Style, font_map: dict[str, Any]) -> str:
    """스타일 → 일본어 폰트 파일명(룰 우선, 없으면 default)."""
    weight = "bold" if style.bold else "regular"
    fam = "serif" if style.serif else "sans"
    for rule in font_map.get("rules", []):
        m = rule.get("match", {})
        if "weight" in m and m["weight"] != weight:
            continue
        if "style" in m and m["style"] != fam:
            continue
        if "size_min" in m and style.font_size < m["size_min"]:
            continue
        if "size_max" in m and style.font_size > m["size_max"]:
            continue
        return rule["jp_font"]
    return font_map.get("default", "NotoSansJP-Bold.ttf")


def wrap_text(text: str, max_chars: int) -> list[str]:
    """CJK 줄바꿈: 공백이 없을 수 있으므로 글자 수 기준 단순 래핑."""
    text = text.strip()
    if not text:
        return []
    if " " in text:  # 공백 있으면 단어 단위 우선
        words, line, out = text.split(), "", []
        for w in words:
            if len(line) + len(w) + 1 > max_chars and line:
                out.append(line)
                line = w
            else:
                line = f"{line} {w}".strip()
        if line:
            out.append(line)
        return out
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def ass_timestamp(seconds: float) -> str:
    """초 → ASS 타임코드 H:MM:SS.cs (반올림 캐리를 분/시까지 전파)."""
    cs_total = int(round(max(0.0, seconds) * 100))   # 먼저 센티초로 반올림 후 분해
    h, rem = divmod(cs_total, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _align_code(position: str) -> int:
    """position 버킷 → ASS numpad alignment(1..9)."""
    v, _, h = position.partition("-")
    row = {"bottom": 0, "center": 3, "top": 6}.get(v, 0)
    col = {"left": 1, "center": 2, "right": 3}.get(h, 2)
    return row + col


def build_ass(events: list[dict[str, Any]], width: int, height: int,
              line_max_chars: int = 16, font_name: str = "Noto Sans JP",
              margin_v: Optional[int] = None) -> str:
    """events: [{start,end,text,position}] → ASS 문자열.

    margin_v: 하단 마진 오버라이드 — 원본 한국어 캡션과의 공존 배치(겹침 회피)용."""
    header = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 0", "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
         "Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"),
        (f"Style: Default,{font_name},{max(24, height // 18)},&H00FFFFFF,&H00000000,"
         f"&H00000000,1,3,0,2,20,20,{margin_v if margin_v else 30},1"), "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    lines = list(header)
    for ev in events:
        wrapped = wrap_text(ev["text"], line_max_chars)
        if not wrapped:
            continue
        text = "\\N".join(wrapped)
        an = _align_code(ev.get("position", "bottom-center"))
        lines.append(
            f"Dialogue: 0,{ass_timestamp(ev['start'])},{ass_timestamp(ev['end'])},"
            f"Default,,0,0,0,,{{\\an{an}}}{text}")
    return "\n".join(lines) + "\n"


def build_bilingual_ass(events: list[dict[str, Any]], width: int, height: int,
                        line_max_chars: int = 16, font_name: str = "Noto Sans JP",
                        position: str = "above", gap_px: int = 8) -> str:
    """한국어 자막은 그대로 두고, 그 위/아래에 일본어를 \\pos 로 덧붙이는 ASS.

    position="above": 한국어 bbox 위에(일본어 하단이 bbox 상단-gap), \\an2(하단중앙) 기준.
    position="below": 한국어 bbox 아래에(일본어 상단이 bbox 하단+gap), \\an8(상단중앙) 기준.
    bbox 없는 이벤트는 화면 상/하단 가장자리에 배치.
    """
    fs = max(20, height // 22)
    header = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 0", "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
         "Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"),
        f"Style: JP,{font_name},{fs},&H0000FFFF,&H00000000,&H00000000,1,3,0,2,10,10,10,1",
        "",  # 일본어=노란색(&H0000FFFF, BGR)로 한국어와 구분
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    lines = list(header)
    for ev in events:
        wrapped = wrap_text(ev["text"], line_max_chars)
        if not wrapped:
            continue
        text = "\\N".join(wrapped)
        bbox = ev.get("bbox")
        if bbox:
            cx = (bbox[0] + bbox[2]) // 2
            if position == "below":
                an, y = 8, min(height - 4, bbox[3] + gap_px)         # 한국어 아래
            else:
                an, y = 2, max(4, bbox[1] - gap_px)                  # 한국어 위
            tag = f"{{\\an{an}\\pos({cx},{y})}}"
        else:   # bbox 없으면 가장자리
            tag = "{\\an8}" if position == "below" else "{\\an2}"
        lines.append(
            f"Dialogue: 0,{ass_timestamp(ev['start'])},{ass_timestamp(ev['end'])},"
            f"JP,,0,0,0,,{tag}{text}")
    return "\n".join(lines) + "\n"


def build_srt(events: list[dict[str, Any]], line_max_chars: int = 16) -> str:
    """events → SRT 문자열(srt 라이브러리 있으면 사용, 없으면 수동)."""
    valid = [e for e in events if e.get("text", "").strip()]
    try:
        import datetime
        import srt

        subs = []
        for i, ev in enumerate(valid, 1):
            subs.append(srt.Subtitle(
                index=i,
                start=datetime.timedelta(seconds=ev["start"]),
                end=datetime.timedelta(seconds=ev["end"]),
                content="\n".join(wrap_text(ev["text"], line_max_chars)),
            ))
        return srt.compose(subs)
    except ImportError:
        return _build_srt_manual(valid, line_max_chars)


def _srt_timestamp(seconds: float) -> str:
    """초 → SRT 타임코드 HH:MM:SS,mmm (반올림 캐리를 분/시까지 전파)."""
    ms_total = int(round(max(0.0, seconds) * 1000))   # 먼저 밀리초로 반올림 후 분해
    h, rem = divmod(ms_total, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _build_srt_manual(events: list[dict[str, Any]], line_max_chars: int) -> str:
    out = []
    for i, ev in enumerate(events, 1):
        body = "\n".join(wrap_text(ev["text"], line_max_chars))
        out.append(f"{i}\n{_srt_timestamp(ev['start'])} --> {_srt_timestamp(ev['end'])}\n{body}\n")
    return "\n".join(out)


# ── 자막 이벤트 생성 (detect+translate → 시간구간 병합) ──────────────────
def detections_to_events(doc: DetectionDoc, tmap: dict[str, str]) -> list[dict[str, Any]]:
    """샘플 프레임의 텍스트를 같은 내용이 이어지는 시간 구간으로 병합."""
    step_t = doc.sample_every / doc.fps if doc.fps else 0.5
    events: list[dict[str, Any]] = []
    active: dict[str, dict[str, Any]] = {}  # source -> open event
    for fr in doc.frames:
        present = set()
        for r in fr.regions:
            src = r.text.strip()
            if not src or src not in tmap or not tmap[src]:
                continue
            present.add(src)
            if src not in active:
                active[src] = {"start": fr.timestamp, "end": fr.timestamp + step_t,
                               "text": tmap[src], "position": r.style.position,
                               "bbox": r.bbox}    # 한국어 자막 위치(일본어 위/아래 배치용)
            else:
                active[src]["end"] = fr.timestamp + step_t
        for src in list(active):
            if src not in present:
                events.append(active.pop(src))
    events.extend(active.values())
    events.sort(key=lambda e: e["start"])
    return events


# ── 모드 A: Pillow 합성 ──────────────────────────────────────────────────
def render_replace(inpainted_dir: str, doc: DetectionDoc, tmap: dict[str, str],
                   config: dict[str, Any], out_dir: str, font_map: dict[str, Any]) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    rcfg = config.get("render", {})
    fonts_dir = resolve_path(config["paths"]["fonts_dir"])
    stroke = int(rcfg.get("stroke_width", 3))
    out = ensure_dir(out_dir)
    step = doc.sample_every
    by_key = {f.frame_idx: f for f in doc.frames}

    def _font(style: Style):
        fp = fonts_dir / resolve_font(style, font_map)
        try:
            return ImageFont.truetype(str(fp), size=max(12, style.font_size))
        except Exception:
            log.warning("폰트 로드 실패(%s) → 기본 폰트. font_map/fonts_dir 확인", fp)
            return ImageFont.load_default()

    frames = sorted(Path(inpainted_dir).glob("*.png"))
    for fp in frames:
        idx = int(fp.stem)
        fd = by_key.get((idx // step) * step)
        img = Image.open(fp).convert("RGB")
        if fd:
            draw = ImageDraw.Draw(img)
            for r in fd.regions:
                jp = tmap.get(r.text.strip(), "")
                if not jp:
                    continue
                lines = wrap_text(jp, int(rcfg.get("line_max_chars", 16)))
                font = _font(r.style)
                bbox_w = r.bbox[2] - r.bbox[0]
                y = r.bbox[1]
                for ln in lines:
                    lw = draw.textlength(ln, font=font)          # bbox 폭 기준 가로 중앙 정렬
                    lx = r.bbox[0] + max(0, (bbox_w - lw) / 2)
                    draw.text((lx, y), ln, font=font, fill=r.style.color,
                              stroke_width=stroke, stroke_fill=r.style.stroke_color)
                    y += int(r.style.font_size * 1.1)
        img.save(out / fp.name)
    log.info("재렌더(replace) %d 프레임 → %s", len(frames), out)
    return out


# ── 오케스트레이션 ───────────────────────────────────────────────────────
def render(doc_path: str, translations_path: str, config: dict[str, Any],
           mode: Optional[str] = None, inpainted_dir: Optional[str] = None,
           out_dir: Optional[str] = None) -> dict[str, str]:
    doc = DetectionDoc.load(doc_path)
    tmap = TranslationDoc.load(translations_path).as_map()
    font_map = load_yaml(resolve_path(config["paths"]["font_map"]))
    mode = mode or config.get("render", {}).get("default_mode", "subtitle")
    base = Path(out_dir) if out_dir else resolve_path(
        f"{config['paths']['outputs_dir']}/{doc.video_id}")
    ensure_dir(base)
    line_max = int(config.get("render", {}).get("line_max_chars", 16))

    # 자막 트랙은 항상 생성(검수·접근성)
    events = detections_to_events(doc, tmap)
    ass_path = base / "ja.ass"
    srt_path = base / "ja.srt"
    ass_path.write_text(build_ass(events, doc.width, doc.height, line_max), encoding="utf-8")
    srt_path.write_text(build_srt(events, line_max), encoding="utf-8")
    result = {"ass": str(ass_path), "srt": str(srt_path)}

    if mode == "replace":
        if not inpainted_dir:
            raise ValueError("replace 모드는 --inpainted (인페인팅된 프레임) 필요")
        result["frames"] = str(render_replace(inpainted_dir, doc, tmap, config,
                                              str(base / "rendered"), font_map))
    elif mode == "bilingual":
        # 한국어 자막 유지 + 그 위/아래에 일본어 추가(인페인팅 없음). 원본 영상에 번인.
        pos = config.get("render", {}).get("overlay_position", "above")
        bi = base / "ja_bilingual.ass"
        bi.write_text(build_bilingual_ass(events, doc.width, doc.height, line_max, position=pos),
                      encoding="utf-8")
        result["bilingual_ass"] = str(bi)
    log.info("렌더 완료(초벌, 검수 전) mode=%s → %s", mode, result)
    return result


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="일본어 재합성/자막 생성")
    p.add_argument("--detections", required=True)
    p.add_argument("--translations", required=True)
    p.add_argument("--config", default=None)
    p.add_argument("--mode", default=None, help="replace|subtitle")
    p.add_argument("--inpainted", default=None, help="replace 모드: 인페인팅된 프레임 디렉토리")
    p.add_argument("--out", default=None)
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    from engine.common import load_config

    args = _parse_args(argv)
    config = load_config(args.config)
    render(args.detections, args.translations, config, mode=args.mode,
           inpainted_dir=args.inpainted, out_dir=args.out)


if __name__ == "__main__":
    main()
