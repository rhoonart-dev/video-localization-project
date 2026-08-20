"""오케스트레이터 — 영상 1편을 엔진①②③ + QA 로 처리한다.

흐름: ffmpeg 추출 → detect → (mask → inpaint) → translate → render
      → ffmpeg 재조립(무손실 FFV1 중간본 → 최종 인코딩) + 원본 오디오 merge → QA.

[필수 게이트] 자동 게시 금지. review_report.md 로 사람 검수 후 통과.
              Level C 더빙은 이 스크립트가 호출하지 않는다(게이트 통과 후 src/dub.py 별도).
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pathlib import Path  # noqa: E402
from typing import Any, Optional  # noqa: E402

from engine import common  # noqa: E402
from engine.common import ensure_dir, get_logger, load_config, resolve_path  # noqa: E402

log = get_logger("process")


def _level_opts(config: dict[str, Any], level: str) -> dict[str, Any]:
    levels = config.get("levels", {})
    if level not in levels:
        raise ValueError(f"알 수 없는 등급: {level} (가능: {list(levels)})")
    return levels[level]


def _apply_subtitle_overrides(work: Path) -> int:
    """검수 반려 '수정 재렌더'(8/14): overrides.json subs{idx: …} 를 translations.json
    entries[idx] 에 반영 — 렌더(자막 그리기)가 이 파일을 읽기 직전에 병합한다.

    idx 는 검수 카드 자막 순번(= entries 순번 — 관제 review_meta 가 translations.json
    순서 그대로 카드에 보여준다). 값이 dict 면 {"ja", "style", "start_sec", "end_sec",
    "use"}(계약: docs/subtitle-style-overrides.md — style·타이밍·use 는 검증 실패 시
    즉시 실패, 조용한 무시 금지), 문자열이면 ja 만. 없는 idx·빈 ja 는 무시. 적용 건수
    반환. 파일이 없으면 0(일반 처리 — 재렌더 아님). entries 에 실린 style/start_sec/
    end_sec 는 render.attach_entry_overrides 가 이벤트로 전사하고, use=false(소프트
    삭제, E6-0)는 TranslationDoc.as_map 이 tmap 에서 빼 번인·ass/srt 전부 제외한다."""
    import json as _json

    from engine.render import validate_line_style, validate_line_timing
    ov_path = work / "overrides.json"
    tr_path = work / "translations.json"
    if not (ov_path.exists() and tr_path.exists()):
        return 0
    try:
        subs = (_json.loads(ov_path.read_text(encoding="utf-8")) or {}).get("subs") or {}
        doc = _json.loads(tr_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("overrides 병합 실패(무시하고 초벌 번역 진행): %s", e)
        return 0
    entries = doc.get("entries") or []
    n = 0
    for key, v in subs.items():
        try:
            i = int(key)
        except (TypeError, ValueError):
            continue
        if not 0 <= i < len(entries):
            continue
        changed = False
        text = v.get("ja") if isinstance(v, dict) else v
        if isinstance(text, str) and text.strip():
            entries[i]["target"] = text.strip()
            changed = True
        if isinstance(v, dict):
            if v.get("style") is not None:
                entries[i]["style"] = validate_line_style(v["style"])   # 위반 = 즉시 실패
                changed = True
            start, end = validate_line_timing(v)
            if start is not None:
                entries[i]["start_sec"] = start
                changed = True
            if end is not None:
                entries[i]["end_sec"] = end
                changed = True
            if "use" in v:                       # 소프트 삭제(E6-0) — false = 그 줄 제외
                if not isinstance(v["use"], bool):
                    raise ValueError(f"subs[{key}].use 는 불리언(false=그 줄 제외): {v['use']!r}")
                entries[i]["use"] = v["use"]
                changed = True
        n += 1 if changed else 0
    if n:
        tr_path.write_text(_json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return n


def process_video(video: str, video_id: str, level: str, config: dict[str, Any],
                  content_type: Optional[str] = None, roi: Optional[tuple] = None,
                  hero: bool = False, use_deepl: bool = False,
                  inpaint_backend: Optional[str] = None) -> dict[str, Any]:
    if not common.has_ffmpeg():
        raise RuntimeError("ffmpeg/ffprobe 필요(시스템 설치). README 참고.")

    from engine import detect as detect_mod
    from engine import inpaint as inpaint_mod
    from engine import mask as mask_mod
    from engine import qa as qa_mod
    from engine import render as render_mod
    from engine import translate as translate_mod

    opts = _level_opts(config, level)
    work = ensure_dir(resolve_path(f"{config['paths']['outputs_dir']}/{video_id}"))
    frames_dir = work / "frames"
    log.info("=== 처리 시작 video_id=%s level=%s content=%s ===", video_id, level, content_type)

    # [1] 추출
    meta = common.probe(video)
    fps = meta["fps"] or 30.0
    common.extract_frames(video, frames_dir)
    total_frames = len(list(frames_dir.glob("*.png")))
    audio = common.extract_audio(video, work / "audio.wav")
    log.info("추출 완료: %d프레임 @ %.3ffps, 오디오=%s", total_frames, fps, bool(audio))

    # [2] 탐지
    doc = detect_mod.detect(video, video_id, config, roi=roi)

    # [3] 마스크 + 인페인팅 (등급에 따라)
    if opts.get("inpaint"):
        masks_dir = mask_mod.build_masks(doc, config, total_frames=total_frames)
        inpainted_dir = inpaint_mod.inpaint(
            str(frames_dir), str(masks_dir), str(work / "inpainted"), config,
            backend_name=inpaint_backend, content_type=content_type)
    else:
        inpainted_dir = frames_dir  # 자막 모드: 화면 텍스트 제거 안 함
        log.info("Level %s: 인페인팅 생략(자막 모드)", level)

    # [4] 번역(초벌) / [5] 재렌더 — clean 모드는 둘 다 생략(캡션 제거만, 더빙이 자막 담당)
    render_mode = opts.get("render_mode", "subtitle")
    if render_mode == "clean":
        render_out = {}
        log.info("clean 모드: 텍스트 재렌더·번역 생략 — 캡션 제거 프레임 그대로(BC: 더빙이 뒤따름)")
    else:
        translate_mod.translate(str(work / "detections.json"), config,
                                hero=hero, use_deepl=use_deepl)
        n_ov = _apply_subtitle_overrides(work)
        if n_ov:
            log.info("반려 수정 병합: 자막 %d건 교체(overrides.json → translations.json)", n_ov)
        render_out = render_mod.render(
            str(work / "detections.json"), str(work / "translations.json"), config,
            mode=render_mode, inpainted_dir=str(inpainted_dir) if render_mode == "replace" else None)

    # [6] 재조립: (무손실 FFV1 중간본 → 최종 인코딩) + 오디오 merge
    final = _reassemble(config, work, fps, render_mode, render_out, inpainted_dir,
                        frames_dir, audio, video)

    # [7] QA 리포트
    report = qa_mod.run_qa(
        video_id, str(frames_dir),
        str(render_out.get("frames", inpainted_dir)) if opts.get("inpaint") else str(frames_dir),
        config, fps=fps,
        extra={"level": level, "content_type": content_type,
               "render_mode": render_mode, "inpaint": bool(opts.get("inpaint")),
               "translation": "초벌(검수 전)"})

    log.warning("게이트: review_report.md 사람 검수 후 통과. 자동 게시 금지(auto_publish=%s).",
                config.get("upload", {}).get("auto_publish", False))
    if level == "C":
        log.info("Level C: 더빙은 게이트 통과 후 `python -m src.dub` 로 별도 실행.")
    result = {"final": str(final), "report": str(report), "translations_draft": True,
              "render": render_out}
    log.info("=== 처리 완료(초벌). 산출물: %s ===", result)
    return result


def _reassemble(config, work: Path, fps: float, render_mode: str, render_out: dict,
                inpainted_dir, frames_dir, audio, src_video) -> Path:
    """프레임 → 무손실 중간본 → 최종 인코딩 + 오디오 merge."""
    enc = config.get("encode", {})
    if render_mode in ("replace", "clean"):     # clean = 캡션 제거 프레임 그대로 조립
        src_frames = render_out.get("frames", str(inpainted_dir))
        intermediate = common.frames_to_video(
            src_frames, work / "intermediate.mkv", fps,
            codec=enc.get("intermediate_codec", "ffv1"))
        encoded = common.frames_to_video(
            src_frames, work / "video_noaudio.mp4", fps,
            codec=enc.get("final_codec", "libx264"),
            pix_fmt=enc.get("pixel_format", "yuv420p"), crf=int(enc.get("final_crf", 18)))
        return common.mux_audio(encoded, audio, work / "final_draft.mp4")
    if render_mode == "bilingual":
        # 번인(2026-08-12 수정): render 는 ja_bilingual.ass 를 만들기만 했고 아무도 굽지 않아,
        # 최종본이 '원본 그대로'였다 — 실측: 혜미리예채파 5화 결과물에 일본어 자막이 없었다.
        # 쇼츠는 사이드카 자막 트랙을 못 쓰므로 여기서 원본 위에 덧입힌다(한국어는 그대로 남는다).
        bi = render_out.get("bilingual_ass")
        if bi and Path(bi).exists():
            fonts = config.get("paths", {}).get("fonts_dir")
            return common.burn_subtitles(
                src_video, bi, work / "final_draft.mp4",
                fonts_dir=resolve_path(fonts) if fonts else None,
                crf=int(enc.get("final_crf", 18)),
                pix_fmt=enc.get("pixel_format", "yuv420p"))
        log.warning("bilingual 인데 ja_bilingual.ass 가 없다 — 원본 그대로 내보낸다(자막 없음)")
    # 자막 모드: 원본 화질 유지 → 원본을 그대로 최종본으로(자막은 sidecar ja.ass/srt)
    log.info("자막 모드: 원본 영상 유지 + ja.ass/ja.srt 사이드카(업로더가 자막 추가/번인).")
    return common.mux_audio(src_video, None, work / "final_draft.mp4")


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="영상 1편 현지화 처리(초벌까지). 자동 게시 금지.")
    p.add_argument("--video", required=True)
    p.add_argument("--video-id", required=True)
    p.add_argument("--level", default="B")   # 검증은 config.levels 기반(_level_opts) — BC 등 확장 라우트 허용
    p.add_argument("--content-type", default=None, help="mukbang|anime|high_motion (인페인트 모드 선택)")
    p.add_argument("--subtitle-area", nargs=4, type=int, metavar=("X1", "Y1", "X2", "Y2"))
    p.add_argument("--backend", default=None, help="인페인트 백엔드 강제(opencv|lama|sttn|propainter)")
    p.add_argument("--hero", action="store_true", help="번역 고품질 모델")
    p.add_argument("--deepl", action="store_true")
    p.add_argument("--config", default=None)
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    config = load_config(args.config)
    roi = tuple(args.subtitle_area) if args.subtitle_area else None
    process_video(args.video, args.video_id, args.level, config,
                  content_type=args.content_type, roi=roi, hero=args.hero,
                  use_deepl=args.deepl, inpaint_backend=args.backend)


if __name__ == "__main__":
    main()
