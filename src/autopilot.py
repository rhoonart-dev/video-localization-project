"""autopilot — 쇼츠 자동 현지화 파이프라인 오케스트레이터 (Phase 1: 선별까지).

Phase 1 범위: 스카우트(scan) → 스코어링(score) → 후보 리포트(report). **업로드 없음.**
처리(selected 이후)와 업로드는 Phase 2 — README 가드(자동 게시 금지)는 그대로 유효하며,
공개 전환 승인은 항상 사람이 한다.

  python -m src.autopilot scan               # 채널 Shorts → 원장(discovered)
  python -m src.autopilot score [--limit 30] # 신규 발견분 스코어링(LLM+공개지표)
  python -m src.autopilot report [--top 10]  # 후보 TOP N 리포트(md+csv)
  python -m src.autopilot status             # 상태별 집계
  python -m src.autopilot mark <id> --state selected|skipped   # 사람 결정 기록
  python -m src.autopilot rescore [id]        # scored → discovered (재채점)

Phase 2 (처리~승인·패키지 — API 감사 전이라 업로드 클릭은 사람이 YouTube Studio 에서):
  python -m src.autopilot process [--limit 3] [--video-id id]  # selected → 다운로드→실측판별→현지화→QA
  python -m src.autopilot pending                              # 승인 대기 목록(산출물 경로 포함)
  python -m src.autopilot approve <id>                         # 승인 → upload_package/ 생성
  python -m src.autopilot uploaded <id> --url <유튜브URL>      # 업로드 완료 기록(사람이 클릭 후)
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from typing import Any, Optional  # noqa: E402

from engine.common import (ensure_dir, get_logger, get_secret, load_config,  # noqa: E402
                           read_json, resolve_path)
from src import jp_score, ledger, scout  # noqa: E402

log = get_logger("autopilot")


# ── 순수: 신호 조립 / 리포트 ──────────────────────────────────────────────
def build_signals(row: dict[str, Any], jp_comment_ratio: Optional[float],
                  llm_item: dict[str, Any], max_views: float) -> dict[str, Optional[float]]:
    """원장 행 + 수집 신호 → combine_scores 입력."""
    return {
        "views": jp_score.log_norm(row.get("view_count") or 0, max_views),
        "like_ratio": jp_score.like_norm(row.get("like_count") or 0,
                                         row.get("view_count") or 0),
        "jp_comments": jp_comment_ratio,
        "llm_jp_fit": jp_score.llm_component(llm_item),
    }


def valid_level(level: Any) -> Optional[str]:
    """LLM level_guess 검증 — A|B|C 외(null·'D'·문자열)는 None(미상)."""
    return level if level in ("A", "B", "C") else None


def qa_verdict(summary: dict[str, Any], gate: dict[str, Any]) -> tuple[str, str]:
    """qa_result.json 요약 → ('pass'|'hold', 사유). hold 도 승인 큐로 가되 리포트에 표기."""
    frames = int(summary.get("frames", 0))
    if frames == 0:
        return "pass", "측정 없음(인페인트 미적용 — 사람 검수는 그대로)"
    flag_ratio = summary.get("flagged", 0) / frames
    ssim = float(summary.get("ssim_avg", 1.0))
    why = f"플래그 {flag_ratio:.0%}, SSIM {ssim}"
    if flag_ratio > float(gate.get("max_flag_ratio", 0.5)):
        return "hold", why
    if ssim < float(gate.get("min_ssim", 0.85)):
        return "hold", why
    return "pass", why


def final_video_for(route: str, base: pathlib.Path) -> Optional[pathlib.Path]:
    """라우트별 최종 산출 영상. A(무변환)는 None — 원본을 그대로 쓴다."""
    names = {"B": ["final_draft.mp4"],
             "C": ["final_dubbed_subbed.mp4", "final_dubbed.mp4"],
             "BC": ["final_dubbed_subbed.mp4", "final_dubbed.mp4"]}.get(route, [])
    for n in names:
        if (base / n).exists():
            return base / n
    return None


def build_upload_text(meta: dict[str, Any], row: dict[str, Any], route: str,
                      qa_note: str = "") -> str:
    """upload_package/UPLOAD.md — 사람이 YouTube Studio 에서 복붙·체크할 전부."""
    lines = [f"# 업로드 패키지 — {row.get('video_id')}", "",
             f"- 원본: {row.get('title')} ({row.get('url')})",
             f"- 처리 라우트: {route} — autopilot 실측 라우팅(B=캡션 교체 / C=더빙 / BC=캡션제거+더빙 / "
             f"A=무변환·메타만). ⚠ README 의 Level A/B/C(process_video 축)와 다른 축이니 "
             f"수동 재처리 시 `src.process_video --level` 로 혼용 금지",
             f"- QA: {qa_note}", "",
             "## 제목 후보 (하나 선택)"]
    for i, t in enumerate(meta.get("title_candidates", []), 1):
        lines.append(f"{i}. {t}")
    lines += ["", "## 설명", "```", meta.get("description", ""), "```",
              "", "## 해시태그 / 태그",
              " ".join(meta.get("hashtags", [])),
              ", ".join(meta.get("tags", [])), "",
              "## 업로드 설정 체크리스트 (YouTube Studio)",
              "- [ ] 공개 설정: 비공개로 올린 뒤 **예약 공개 19:00 JST** (config upload.default_time)",
              "- [ ] 언어: defaultAudioLanguage=ja (더빙본) / 제목·설명 언어 ja",
              "- [ ] 시청자층: madeForKids 여부 — **채널 정책 결정대로** (미결정이면 게시 보류)",
              "- [ ] 라이선스/저작권 표기: 설명란 © 라인 확인",
              "- [ ] 최종 영상·자막을 눈과 귀로 검수(게이트①②③) 후 게시", ""]
    return "\n".join(lines)


def _esc(s: Any) -> str:
    return str(s if s is not None else "").replace("|", "\\|")


def build_report_md(rows: list[dict[str, Any]], generated_at: str) -> str:
    """후보 TOP N → 사람이 읽는 마크다운 리포트."""
    head = [f"# autopilot 후보 리포트 — {generated_at}", "",
            "> Phase 1: 이 리포트는 **후보 제안까지만**이다 — 어떤 것도 업로드하지 않는다.",
            "> 처리 시작은 사람이 결정: `python -m src.autopilot mark <video_id> --state selected`",
            ""]
    if not rows:
        return "\n".join(head + ["후보 없음 — 먼저 `scan` 후 `score` 를 실행하세요.", ""])
    head += ["| 순위 | video_id | 제목 | 길이(s) | 조회수 | 점수 | 레벨(추정) | 근거 |",
             "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        try:
            detail = json.loads(r.get("scores") or "{}")
        except (TypeError, ValueError):
            detail = {}
        reason = detail.get("llm_reason", "")
        head.append(
            f"| {i} | [{r['video_id']}]({_esc(r.get('url'))}) | {_esc(r.get('title'))} "
            f"| {r.get('duration') if r.get('duration') is not None else '?'} "
            f"| {r.get('view_count') if r.get('view_count') is not None else '?'} "
            f"| {r.get('score')} | {r.get('level_guess') or '?'} | {_esc(reason)} |")
    ex = rows[0]["video_id"]
    head += ["", "다음 단계 (예):", "```",
             f"python -m src.autopilot mark {ex} --state selected",
             "```",
             "레벨(추정)은 제목 기반 — 처리 전 프레임 검사로 번인 자막 유무를 반드시 실측할 것",
             "(번인 없는 Short 에 Level B 를 돌리면 OCR 오검출 — 2026-07-01 loopy_short 사례).", ""]
    return "\n".join(head)


# ── 커맨드 ───────────────────────────────────────────────────────────────
def cmd_scan(config: dict[str, Any]) -> int:
    rows = scout.scout(config)
    conn = ledger.connect(config=config)
    try:
        new = ledger.upsert_discovered(conn, rows)
    finally:
        conn.close()
    log.info("scan 완료: 수집 %d편, 신규 %d편", len(rows), new)
    return new


def cmd_score(config: dict[str, Any], limit: Optional[int] = None) -> int:
    ap = config.get("autopilot", {})
    limit = limit or int(ap.get("max_score_per_run", 30))
    api_key = get_secret("YOUTUBE_API_KEY")
    conn = ledger.connect(config=config)
    try:
        todo = ledger.get_by_state(conn, "discovered", limit=limit)
        if not todo:
            log.info("스코어링 대상 없음(discovered 0)")
            return 0
        # 정규화 기준은 원장 전체 최대 조회수(스캔 표본 안에서 상대 평가)
        row = conn.execute("SELECT MAX(view_count) AS m FROM videos").fetchone()
        max_views = float(row["m"] or 0)

        llm_map = jp_score.llm_score_batch(todo, config)
        if not llm_map and ap.get("require_llm", True):
            # LLM 전면 장애(키 미설정·429 등)로 정량 신호만 채점되면 열화 점수가
            # scored 로 고착된다 → 이번 실행은 중단하고 discovered 로 남겨 재시도.
            log.warning("LLM 스코어링 전면 실패 → 실행 중단(%d편 discovered 유지). "
                        "정량 신호만으로 채점하려면 config autopilot.require_llm=false", len(todo))
            return 0
        weights = ap.get("weights", {})
        sample = int(ap.get("comment_sample", 100))
        done = 0
        for r in todo:
            vid = r["video_id"]
            jp_ratio = None
            if api_key and (r.get("comment_count") or 0) > 0:
                try:
                    texts = scout.fetch_comment_texts(vid, api_key, sample)
                except scout.QuotaExceeded as e:
                    log.warning("%s → 실행 중단(잔여 %d편은 다음 실행에서 재시도)",
                                e, len(todo) - done)
                    break
                jp_ratio = jp_score.kana_ratio(texts) if texts is not None else None
            llm_item = llm_map.get(vid, {})
            signals = build_signals(r, jp_ratio, llm_item, max_views)
            total = jp_score.combine_scores(signals, weights)
            detail = {**signals, "llm_reason": llm_item.get("reason", "")}
            ledger.record_score(conn, vid, total, detail,
                                level_guess=valid_level(llm_item.get("level_guess")))
            done += 1
        log.info("score 완료: %d편 (LLM 응답 %d)", done, len(llm_map))
        return done
    finally:
        conn.close()


def cmd_report(config: dict[str, Any], top_n: Optional[int] = None) -> pathlib.Path:
    ap = config.get("autopilot", {})
    top_n = top_n or int(ap.get("report_top_n", 10))
    conn = ledger.connect(config=config)
    try:
        rows = ledger.top_scored(conn, top_n)
    finally:
        conn.close()
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    out_dir = ensure_dir(resolve_path(config["paths"]["outputs_dir"]))
    md_path = out_dir / "autopilot_report.md"
    md_path.write_text(build_report_md(rows, now), encoding="utf-8")
    csv_path = out_dir / "autopilot_report.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        fields = ["video_id", "title", "url", "duration", "view_count",
                  "like_count", "comment_count", "score", "level_guess", "scores"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})
    log.info("리포트: %s (+csv) — 후보 %d편", md_path, len(rows))
    return md_path


def cmd_status(config: dict[str, Any]) -> dict[str, int]:
    conn = ledger.connect(config=config)
    try:
        c = ledger.counts(conn)
    finally:
        conn.close()
    for state in ledger.STATES:
        if state in c:
            print(f"{state:18} {c[state]}")
    print(f"{'total':18} {sum(c.values())}")
    return c


def cmd_mark(config: dict[str, Any], video_id: str, state: str,
             notes: Optional[str] = None) -> None:
    if state not in ("selected", "skipped"):
        raise SystemExit("mark 는 selected|skipped 만 허용 (그 외 상태는 파이프라인이 관리)")
    conn = ledger.connect(config=config)
    try:
        ledger.set_state(conn, video_id, state, notes=notes)   # 전이 규칙은 원장이 검증
    finally:
        conn.close()
    log.info("mark: %s → %s", video_id, state)


# ── Phase 2: 처리 ~ 승인·패키지 ──────────────────────────────────────────
def _srt_texts(path: pathlib.Path) -> list[str]:
    """SRT 에서 대사 줄만(번호·타임코드 제외)."""
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if s and not s.isdigit() and "-->" not in s:
            out.append(s)
    return out


def _content_context(base: pathlib.Path, pre: dict[str, Any]) -> str:
    """메타데이터 LLM 에 줄 '실측 내용' 컨텍스트 — 제목만으로 지어내는 것 방지."""
    parts = [f"[실측] 화면 번인 자막 {pre.get('burn_frames', 0)}프레임, "
             f"대사 {pre.get('dialogue_segs', 0)}세그먼트."]
    lines = _srt_texts(base / "ja_dub.srt") or _srt_texts(base / "ja.srt")
    if lines:
        parts.append("영상 대사/자막(일본어): " + " / ".join(lines[:20]))
    else:
        parts.append("대사·화면 텍스트 없음 — 비언어(모션·사운드) 영상. 내용을 지어내지 말 것.")
    return "\n".join(parts)


def _download(row: dict[str, Any], config: dict[str, Any]) -> pathlib.Path:
    """원본 Short 다운로드(yt-dlp) → data/source/auto/<id>.mp4 (있으면 재사용)."""
    import subprocess
    ap = config.get("autopilot", {})
    out_dir = ensure_dir(resolve_path(ap.get("download_dir", "data/source/auto")))
    out = out_dir / f"{row['video_id']}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "-q",
           "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
           "--merge-output-format", "mp4", "-o", str(out), row["url"]]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if res.returncode != 0 or not out.exists():
        raise RuntimeError(f"다운로드 실패({row['video_id']}): {res.stderr[-300:]}")
    return out


def _dub_cmd(dub_py: str, video: str, video_id: str,
             config_path: Optional[str] = None) -> list[str]:
    """dub subprocess 인자 — `--opt=value` 형태(하이픈으로 시작하는 video_id 안전)."""
    cmd = [dub_py, "-m", "src.dub", f"--video-id={video_id}",
           f"--video={video}", "--level=C"]
    if config_path:                        # 커스텀 config 를 서브프로세스에도 전파
        cmd.append(f"--config={config_path}")
    return cmd


def _run_dub(video: pathlib.Path, video_id: str, config: dict[str, Any],
             config_path: Optional[str] = None) -> None:
    """Level C(더빙) — GPT-SoVITS 스택은 .venv-gsv(파이썬 3.11) 전용이라 subprocess."""
    import os
    import subprocess
    ap = config.get("autopilot", {})
    dub_py = resolve_path(ap.get("dub_python", ".venv-gsv/bin/python"))
    if not pathlib.Path(dub_py).exists():
        raise RuntimeError(f"더빙 인터프리터 없음: {dub_py} (config autopilot.dub_python)")
    env = {**os.environ, "is_half": "False", "TERM": "xterm"}
    res = subprocess.run(_dub_cmd(str(dub_py), str(video), video_id, config_path),
                         capture_output=True, text=True, timeout=3600,
                         cwd=str(resolve_path(".")), env=env)
    if res.returncode != 0:
        raise RuntimeError(f"더빙 실패({video_id}): {res.stderr[-400:]}")


def cmd_process(config: dict[str, Any], limit: Optional[int] = None,
                video_id: Optional[str] = None,
                config_path: Optional[str] = None) -> int:
    """selected → 다운로드 → 실측 판별(precheck) → 라우트별 현지화 → QA → 승인 대기."""
    from src import precheck as precheck_mod
    ap = config.get("autopilot", {})
    conn = ledger.connect(config=config)
    try:
        stale = ledger.get_by_state(conn, "processing")
        if stale:   # 중단(Ctrl-C·전원)으로 남은 고아 — 복구 경로 안내
            log.warning("processing 고아 %d편: %s — `process --video-id <id>` 로 재개 가능",
                        len(stale), ", ".join(s["video_id"] for s in stale[:5]))
        if video_id:
            row = conn.execute("SELECT * FROM videos WHERE video_id=?", (video_id,)).fetchone()
            if row is None:
                raise SystemExit(f"원장에 없는 video_id: {video_id}")
            rows = [dict(row)]
        else:
            rows = ledger.get_by_state(conn, "selected", limit=limit or int(ap.get("max_process_per_run", 3)))
        if not rows:
            log.info("처리 대상 없음(selected 0) — 먼저 mark <id> --state selected")
            return 0
        done = 0
        for r in rows:
            vid = r["video_id"]
            ledger.set_state(conn, vid, "processing")
            try:
                video = _download(r, config)
                pre = precheck_mod.precheck(str(video), vid, config)
                route = pre["route"]
                conn.execute("UPDATE videos SET level_guess=? WHERE video_id=?", (route, vid))
                conn.commit()

                base = resolve_path(f"{config['paths']['outputs_dir']}/{vid}")
                # 이전 실행(다른 라우트)의 QA 잔재가 이번 판정을 오염시키지 않게 제거
                (base / "qa_result.json").unlink(missing_ok=True)
                if route == "B":
                    from src.process_video import process_video
                    process_video(str(video), vid, "B", config,
                                  content_type=ap.get("content_type", "anime"),
                                  inpaint_backend=ap.get("inpaint_backend", "opencv"))
                elif route == "C":
                    _run_dub(video, vid, config, config_path)
                elif route == "BC":
                    # 먹방류: 캡션 제거(clean) 후, 제거된 영상 위에 더빙+일본어 자막
                    from src.process_video import process_video
                    process_video(str(video), vid, "BC", config,
                                  content_type=ap.get("content_type", "anime"),
                                  inpaint_backend=ap.get("inpaint_backend", "opencv"))
                    _run_dub(base / "final_draft.mp4", vid, config, config_path)
                # route A: 영상 무변환 — 메타데이터만

                # 메타데이터: 제목만 주면 LLM 이 내용을 지어낸다(E2E 실측) →
                # 실측 대사/자막을 컨텍스트로 전달해 실제 내용에 맞게 생성.
                from src.metadata import generate
                generate(vid, r.get("title") or "", _content_context(base, pre), config)
                summary = {"frames": 0}
                if route in ("B", "BC"):              # QA 는 인페인트 비교가 있는 라우트만 의미
                    try:
                        summary = read_json(base / "qa_result.json")
                    except Exception:                 # 부재·손상 → 측정 없음 폴백
                        log.warning("qa_result.json 읽기 실패(%s) — 측정 없음 처리", vid)
                verdict, why = qa_verdict(summary, ap.get("qa_gate", {}))
                ledger.set_state(conn, vid, "qa_passed",
                                 notes=f"route={route}; qa={verdict}: {why}")
                ledger.set_state(conn, vid, "pending_approval")
                done += 1
                log.info("처리 완료: %s (route=%s, qa=%s) → 승인 대기", vid, route, verdict)
                from src.notify import notify
                final = final_video_for(route, base)
                notify(f"✅ 처리 완료 [{route}] {r.get('title')}\n"
                       f"검수: {final or '(무변환 — 원본)'}\n"
                       f"승인: `python -m src.autopilot approve {vid}`")
            except BaseException as e:                # Ctrl-C 도 원장에 기록 후 전파
                log.exception("처리 실패: %s", vid)
                ledger.set_state(conn, vid, "failed",
                                 notes=str(e)[:300] or type(e).__name__, force=True)
                from src.notify import notify
                notify(f"❌ 처리 실패 {vid} ({r.get('title')}): {str(e)[:200]}")
                if not isinstance(e, Exception):      # KeyboardInterrupt/SystemExit
                    raise
        log.info("process 완료: %d/%d편 → pending_approval. 다음: pending / approve <id>",
                 done, len(rows))
        return done
    finally:
        conn.close()


def cmd_pending(config: dict[str, Any]) -> list[dict[str, Any]]:
    """승인 대기 목록 — 검수할 산출물 경로와 함께."""
    conn = ledger.connect(config=config)
    try:
        rows = ledger.get_by_state(conn, "pending_approval")
    finally:
        conn.close()
    if not rows:
        print("승인 대기 없음.")
        return rows
    out_dir = resolve_path(config["paths"]["outputs_dir"])
    for r in rows:
        route = (r.get("level_guess") or "?")
        final = final_video_for(route, out_dir / r["video_id"])
        if final is None:                              # B/C 산출물 누락 ≠ 무변환 — 구분 표시
            final = ("(무변환 — 원본 사용)" if route == "A"
                     else "(⚠ 산출물 없음 — process --video-id 재실행 필요)")
        print(f"{r['video_id']}  [{route}]  {r.get('title')}")
        print(f"    검수: {final}  |  {r.get('notes')}")
        print(f"    승인: python -m src.autopilot approve {r['video_id']}")
    return rows


def cmd_approve(config: dict[str, Any], video_id: str) -> pathlib.Path:
    """승인 → upload_package/ 생성(영상+메타+자막+체크리스트). 업로드 클릭은 사람이.

    패키지 생성이 전부 성공한 뒤에만 approved 로 전이 — 실패 시 pending_approval 에
    남아 pending 목록에서 보이고 재시도 가능."""
    import shutil
    ap = config.get("autopilot", {})
    conn = ledger.connect(config=config)
    try:
        row = conn.execute("SELECT * FROM videos WHERE video_id=?", (video_id,)).fetchone()
        if row is None:
            raise SystemExit(f"원장에 없는 video_id: {video_id}")
        row = dict(row)

        base = resolve_path(f"{config['paths']['outputs_dir']}/{video_id}")
        route = row.get("level_guess") or "A"
        final = final_video_for(route, base)
        if final is None:
            if route != "A":   # B/C 산출물 누락 — 한국어 원본을 일본어본으로 포장하면 안 됨
                raise SystemExit(
                    f"산출 영상 없음(route={route}): {base} — "
                    f"`process --video-id {video_id}` 재실행 후 승인하세요")
            final = resolve_path(ap.get("download_dir", "data/source/auto")) / f"{video_id}.mp4"
            if not final.exists():
                raise SystemExit(f"원본 영상 없음: {final} — process 먼저")

        pkg = ensure_dir(base / "upload_package")
        shutil.copy2(final, pkg / f"{video_id}_ja.mp4")
        # 자막은 라우트에 맞는 것만 — 다른 라우트의 잔재(예: C→A 재처리 후 낡은 ja_dub.srt) 배제
        for srt in {"B": ["ja.srt"], "C": ["ja_dub.srt"]}.get(route, []):
            if (base / srt).exists():
                shutil.copy2(base / srt, pkg / srt)
        for stale in set(("ja.srt", "ja_dub.srt")) - set(
                {"B": ["ja.srt"], "C": ["ja_dub.srt"]}.get(route, [])):
            (pkg / stale).unlink(missing_ok=True)
        meta_path = base / "metadata_draft.json"
        meta = read_json(meta_path) if meta_path.exists() else {}
        (pkg / "UPLOAD.md").write_text(
            build_upload_text(meta, row, route, qa_note=row.get("notes") or ""),
            encoding="utf-8")

        ledger.set_state(conn, video_id, "approved")   # 패키지 완성 후에만 전이
    finally:
        conn.close()
    if config.get("upload", {}).get("api_upload", False):
        # 승인 = 사람의 공개 결정 → 이후 업로드·예약은 자동(실패 시 approved 유지, upload 로 재시도)
        log.info("승인 완료 → API 자동 업로드 진행")
        print(f"업로드 패키지: {pkg}")
        cmd_upload(config, video_id)
        return pkg
    log.info("승인 완료 → 업로드 패키지: %s (업로드 클릭은 사람이 — YouTube Studio)", pkg)
    from src.notify import notify
    notify(f"📦 승인 완료 — {row.get('title')}\n패키지: {pkg}\n"
           f"업로드는 YouTube Studio 에서(UPLOAD.md 체크리스트 참고)")
    print(f"업로드 패키지: {pkg}")
    return pkg


def cmd_upload(config: dict[str, Any], video_id: str) -> dict[str, Any]:
    """approved 영상을 API 로 업로드(private + publishAt 예약 공개).

    공개 '결정'은 approve(사람)가 이미 했다 — 여기는 기계적 실행만.
    실패 시 approved 에 남아 재시도 가능(`upload <id>`)."""
    from datetime import datetime, timezone as _tz
    from src import uploader
    from src.notify import notify

    ucfg = config.get("upload", {})
    conn = ledger.connect(config=config)
    try:
        row = conn.execute("SELECT * FROM videos WHERE video_id=?", (video_id,)).fetchone()
        if row is None:
            raise SystemExit(f"원장에 없는 video_id: {video_id}")
        row = dict(row)
        if row["state"] != "approved":
            raise SystemExit(f"업로드는 approved 상태에서만 (현재: {row['state']}) — approve 먼저")

        base = resolve_path(f"{config['paths']['outputs_dir']}/{video_id}")
        pkg_video = base / "upload_package" / f"{video_id}_ja.mp4"
        if not pkg_video.exists():
            raise SystemExit(f"패키지 영상 없음: {pkg_video} — approve 를 다시 실행")
        meta_path = base / "metadata_draft.json"
        draft = read_json(meta_path) if meta_path.exists() else {}

        publish_at = uploader.next_publish_at(
            datetime.now(_tz.utc), ledger.taken_publish_slots(conn),
            hhmm=str(ucfg.get("default_time", "19:00")),
            tz_name=str(ucfg.get("timezone", "Asia/Tokyo")))
        body = uploader.build_upload_meta(draft, row, row.get("level_guess") or "A",
                                          publish_at, ucfg)
        yt_id = uploader.upload_video(pkg_video, body)
        ledger.record_upload(conn, video_id, yt_id, publish_at)
    finally:
        conn.close()
    url = f"https://youtu.be/{yt_id}"
    notify(f"🚀 업로드 완료(예약 공개) — {row.get('title')}\n"
           f"{url}\n공개 시각: {publish_at} (그 전까지 비공개 — Studio 에서 수정/취소 가능)")
    log.info("업로드·예약 완료: %s → %s (publishAt=%s)", video_id, url, publish_at)
    print(f"업로드 완료: {url} (예약 공개 {publish_at})")
    return {"youtube_id": yt_id, "publish_at": publish_at, "url": url}


def cmd_refbank(config: dict[str, Any], action: str,
                sources: Optional[list[str]] = None) -> None:
    """레퍼런스 음성 은행 관리 — status(현황) | seed(대사 소스에서 축적).

    seed 인자는 media 경로 또는 'path:source_id'. 보컬 분리본이면 자동 감지(vocals in name)."""
    from src import refbank
    if action == "status":
        st = refbank.bank_status(config)
        print(f"은행 클립 {st['clips']}개, 소스: {', '.join(st['sources']) or '없음'}")
        return
    if action == "seed":
        total = 0
        for spec in (sources or []):
            path, _, sid = spec.partition(":")
            src_id = sid or pathlib.Path(path).stem
            is_voc = "vocal" in pathlib.Path(path).name.lower()
            try:
                n = refbank.harvest(str(resolve_path(path)), config, src_id, is_vocals=is_voc)
                total += n
                log.info("seed %s → %d클립", src_id, n)
            except Exception as e:  # noqa: BLE001
                log.warning("seed 실패 %s: %s", path, e)
        print(f"은행 축적 완료: +{total}클립")
        return
    raise SystemExit("refbank action 은 status | seed")


def cmd_uploaded(config: dict[str, Any], video_id: str, url: Optional[str] = None) -> None:
    """사람이 업로드를 마친 뒤 기록 — approved → uploaded (종착)."""
    conn = ledger.connect(config=config)
    try:
        ledger.set_state(conn, video_id, "uploaded", notes=url)
    finally:
        conn.close()
    log.info("uploaded 기록: %s (%s)", video_id, url or "URL 미기재")


def cmd_daily(config: dict[str, Any], config_path: Optional[str] = None) -> None:
    """launchd 일일 실행: scan → score → process(selected 있으면) → report → Slack 다이제스트.

    사람의 리듬: Slack 다이제스트 보고 후보를 mark selected → 다음 daily 가 처리 →
    승인 알림 오면 검수 후 approve → Studio 업로드."""
    from src.notify import build_digest, notify
    try:
        cmd_scan(config)
        cmd_score(config)
        cmd_process(config, config_path=config_path)
        cmd_report(config)
        conn = ledger.connect(config=config)
        try:
            counts = ledger.counts(conn)
            top = ledger.top_scored(conn, 5)
            pending = ledger.get_by_state(conn, "pending_approval")
        finally:
            conn.close()
        notify(build_digest(counts, top, pending))
    except BaseException as e:                        # 실패도 반드시 사람에게
        notify(f"🔴 autopilot daily 실패: {type(e).__name__}: {str(e)[:300]}")
        raise


def cmd_rescore(config: dict[str, Any], video_id: Optional[str] = None) -> int:
    """scored → discovered 리셋(재스코어). 전량 재스캔 후 정규화 기준이 바뀌었거나
    LLM 부분 장애로 열화 채점된 배치를 다음 score 실행에서 다시 채점하게 한다."""
    conn = ledger.connect(config=config)
    try:
        targets = ([{"video_id": video_id}] if video_id
                   else ledger.get_by_state(conn, "scored"))
        for r in targets:
            ledger.set_state(conn, r["video_id"], "discovered", notes="rescore")
    finally:
        conn.close()
    log.info("rescore: %d편 → discovered (다음 score 에서 재채점)", len(targets))
    return len(targets)


# ── CLI ──────────────────────────────────────────────────────────────────
def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="쇼츠 자동 현지화 — Phase 1 선별 봇(업로드 없음)")
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    ps = sub.add_parser("score")
    ps.add_argument("--limit", type=int, default=None)
    pr = sub.add_parser("report")
    pr.add_argument("--top", type=int, default=None)
    sub.add_parser("status")
    pm = sub.add_parser("mark")
    pm.add_argument("video_id")
    pm.add_argument("--state", required=True, choices=["selected", "skipped"])
    pm.add_argument("--notes", default=None)
    pc = sub.add_parser("rescore")
    pc.add_argument("video_id", nargs="?", default=None)
    pp = sub.add_parser("process")
    pp.add_argument("--limit", type=int, default=None)
    pp.add_argument("--video-id", default=None)
    sub.add_parser("pending")
    pa = sub.add_parser("approve")
    pa.add_argument("video_id")
    pu = sub.add_parser("uploaded")
    pu.add_argument("video_id")
    pu.add_argument("--url", default=None)
    sub.add_parser("daily")
    pl = sub.add_parser("upload")
    pl.add_argument("video_id")
    prb = sub.add_parser("refbank")
    prb.add_argument("action", choices=["status", "seed"])
    prb.add_argument("sources", nargs="*", help="seed: media 경로 또는 path:source_id")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    config = load_config(args.config)
    if args.cmd == "scan":
        cmd_scan(config)
    elif args.cmd == "score":
        cmd_score(config, args.limit)
    elif args.cmd == "report":
        cmd_report(config, args.top)
    elif args.cmd == "status":
        cmd_status(config)
    elif args.cmd == "mark":
        cmd_mark(config, args.video_id, args.state, args.notes)
    elif args.cmd == "rescore":
        cmd_rescore(config, args.video_id)
    elif args.cmd == "process":
        cmd_process(config, args.limit, args.video_id, config_path=args.config)
    elif args.cmd == "pending":
        cmd_pending(config)
    elif args.cmd == "approve":
        cmd_approve(config, args.video_id)
    elif args.cmd == "uploaded":
        cmd_uploaded(config, args.video_id, args.url)
    elif args.cmd == "daily":
        cmd_daily(config, config_path=args.config)
    elif args.cmd == "upload":
        cmd_upload(config, args.video_id)
    elif args.cmd == "refbank":
        cmd_refbank(config, args.action, args.sources)


if __name__ == "__main__":
    main()
