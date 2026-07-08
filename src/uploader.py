"""YouTube 자동 업로더 — 승인(approve, 사람) 이후의 기계적 업로드·예약만 담당.

원칙: 공개 '결정'은 여전히 사람(approve)이 한다. 이 모듈은 결정된 것을
private + publishAt(예약 공개)로 올리는 실행자일 뿐이다.

전제(2026-07-08 실측): 이 GCP 프로젝트는 감사 없이도 업로드·공개 전환이 동작함
(scripts/yt_upload_test.py 로 검증 — 공식 문서의 private 잠금이 발동하지 않음.
대량 업로드 시 재확인 권장). OAuth 데스크톱 클라이언트(.env) + 리프레시 토큰
(outputs/yt_oauth_token.json, 최초 1회 yt_upload_test.py 인증으로 생성).
"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from typing import Any, Optional  # noqa: E402

from engine.common import get_logger, get_secret, resolve_path  # noqa: E402

log = get_logger("uploader")

TOKEN_CACHE = "outputs/yt_oauth_token.json"


# ── 순수: 예약 슬롯 / 메타 ────────────────────────────────────────────────
def next_publish_at(now_utc: datetime, taken: set[str], hhmm: str = "19:00",
                    tz_name: str = "Asia/Tokyo", min_lead_h: float = 1.0) -> str:
    """다음 빈 일일 슬롯(RFC3339 UTC). 하루 1편 페이스 — 잡힌 슬롯은 다음 날로.

    YPP inauthentic content 리스크 회피: 같은 시각 대량 공개 대신 날짜 분산."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    hh, mm = (int(x) for x in hhmm.split(":"))
    local = now_utc.astimezone(tz)
    slot = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    for _ in range(370):                                   # 1년 내 빈 슬롯은 반드시 존재
        slot_utc = slot.astimezone(timezone.utc)
        iso = slot_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        if slot_utc >= now_utc + timedelta(hours=min_lead_h) and iso not in taken:
            return iso
        slot += timedelta(days=1)
    raise RuntimeError("빈 예약 슬롯을 찾지 못함(1년 초과)")


def build_upload_meta(meta_draft: dict[str, Any], row: dict[str, Any], route: str,
                      publish_at: str, ucfg: dict[str, Any]) -> dict[str, Any]:
    """metadata_draft + 원장 행 → videos.insert body. 제목은 1안 자동(사람이 approve 로 승인함)."""
    candidates = meta_draft.get("title_candidates") or []
    title = (candidates[0] if candidates else row.get("title") or row.get("video_id", ""))[:100]
    snippet: dict[str, Any] = {
        "title": title,
        "description": (meta_draft.get("description") or "")[:4900],
        "tags": (meta_draft.get("tags") or [])[:20],
        "categoryId": str(ucfg.get("category_id", "24")),   # 24=Entertainment
        "defaultLanguage": "ja",
    }
    if route == "C":                                        # 더빙본만 오디오 언어 ja
        snippet["defaultAudioLanguage"] = "ja"
    return {"snippet": snippet,
            "status": {"privacyStatus": "private",          # 예약 공개는 private 전제
                       "publishAt": publish_at,
                       "selfDeclaredMadeForKids": bool(ucfg.get("made_for_kids", False))}}


# ── OAuth / API ──────────────────────────────────────────────────────────
def get_access_token() -> str:
    cache_path = resolve_path(TOKEN_CACHE)
    if not cache_path.exists():
        raise RuntimeError(f"업로드 토큰 없음: {cache_path} — "
                           "`python scripts/yt_upload_test.py` 로 최초 1회 인증 필요")
    cache = json.loads(cache_path.read_text())
    data = urllib.parse.urlencode({
        "client_id": get_secret("YT_OAUTH_CLIENT_ID", required=True),
        "client_secret": get_secret("YT_OAUTH_CLIENT_SECRET", required=True),
        "refresh_token": cache["refresh_token"],
        "grant_type": "refresh_token"}).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.loads(r.read().decode())
    if "access_token" not in tok:
        raise RuntimeError(f"토큰 리프레시 실패: {tok} — 재인증 필요(yt_upload_test.py)")
    return tok["access_token"]


def upload_video(video_path: str | pathlib.Path, body: dict[str, Any],
                 token: Optional[str] = None) -> str:
    """resumable 업로드(init→bytes) → YouTube video id."""
    token = token or get_access_token()
    video_path = pathlib.Path(video_path)
    size = video_path.stat().st_size

    init = urllib.request.Request(
        "https://www.googleapis.com/upload/youtube/v3/videos"
        "?part=snippet,status&uploadType=resumable",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Type": "video/mp4",
                 "X-Upload-Content-Length": str(size)})
    try:
        with urllib.request.urlopen(init, timeout=60) as r:
            session_url = r.headers.get("Location")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"업로드 세션 실패 HTTP {e.code}: {e.read().decode()[:400]}") from e
    if not session_url:
        raise RuntimeError("업로드 세션 URL 없음")

    put = urllib.request.Request(session_url, data=video_path.read_bytes(), method="PUT",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "video/mp4"})
    try:
        with urllib.request.urlopen(put, timeout=600) as r:
            res = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"업로드 실패 HTTP {e.code}: {e.read().decode()[:400]}") from e
    vid = res.get("id")
    if not vid:
        raise RuntimeError(f"업로드 응답에 id 없음: {json.dumps(res)[:300]}")
    log.info("업로드 완료: https://youtu.be/%s (private, publishAt=%s)",
             vid, body["status"].get("publishAt"))
    return vid
