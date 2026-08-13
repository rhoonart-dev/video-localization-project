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
