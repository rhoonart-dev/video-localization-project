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

from engine.common import ensure_dir, get_logger, get_secret, load_config, resolve_path  # noqa: E402
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


if __name__ == "__main__":
    main()
