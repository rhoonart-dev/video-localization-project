"""Slack 알림 — autopilot 이 사람에게 말 거는 채널 (incoming webhook).

시크릿은 .env 의 SLACK_WEBHOOK_URL (커밋 금지). 미설정이면 조용히 no-op —
알림 장애가 파이프라인을 죽이지 않는다(로그만).
"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from typing import Any, Optional  # noqa: E402

from engine.common import get_logger, get_secret  # noqa: E402

log = get_logger("notify")


def notify(text: str, webhook: Optional[str] = None) -> bool:
    """Slack 전송. 성공 True / 미설정·실패 False (예외를 밖으로 던지지 않는다)."""
    url = webhook or get_secret("SLACK_WEBHOOK_URL")
    if not url:
        log.info("SLACK_WEBHOOK_URL 미설정 — 알림 생략: %s", text[:80])
        return False
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = resp.status == 200
        if not ok:
            log.warning("Slack 응답 비정상: %s", resp.status)
        return ok
    except Exception as e:
        log.warning("Slack 알림 실패(무시하고 계속): %s", e)
        return False


def build_digest(counts: dict[str, int], top: list[dict[str, Any]],
                 pending: list[dict[str, Any]]) -> str:
    """일일 다이제스트 — 상태 집계 + 후보 TOP + 승인 대기 + 다음 행동."""
    lines = ["*🤖 loopy-jp autopilot 일일 리포트*", "",
             "상태: " + (" / ".join(f"{k} {v}" for k, v in counts.items()) or "원장 비어 있음"), ""]
    if top:
        lines.append("*후보 TOP (점수순)* — 처리하려면 `mark <id> --state selected`")
        for i, r in enumerate(top, 1):
            views = f"{r.get('view_count'):,}" if r.get("view_count") else "?"
            lines.append(f"{i}. <{r.get('url')}|{r.get('title')}> — 조회 {views}, 점수 {r.get('score')}")
        lines.append(f"예: `python -m src.autopilot mark {top[0]['video_id']} --state selected`")
    else:
        lines.append("후보 없음 — scan/score 확인 필요")
    lines.append("")
    if pending:
        lines.append("*🟡 승인 대기* — 검수 후 `approve <id>`")
        for r in pending:
            lines.append(f"- [{r.get('level_guess') or '?'}] {r.get('title')} "
                         f"(`python -m src.autopilot approve {r['video_id']}`)")
    else:
        lines.append("승인 대기 없음")
    return "\n".join(lines)
