"""채널 스카우트 — 원 채널(잔망루피)의 Shorts 목록·공개 지표 수집 (autopilot Phase 1).

백엔드 2종 (config.autopilot.scout_backend):
  api   — YouTube Data API v3 (YOUTUBE_API_KEY). 정확한 조회수·좋아요·길이.
          channels.list(forHandle) → UUSH Shorts 플레이리스트 → playlistItems → videos.list.
          채널 전량(~1,100편)도 ~46유닛(일일 무료쿼터의 0.5%).
  ytdlp — yt-dlp --flat-playlist (키 불필요). 조회수는 근사치, 길이 미상.
          ⚠ ToS 그레이존 + 고빈도 시 차단 리스크 → 저빈도 보조용.
  auto  — 키 있으면 api, 없으면 ytdlp.

수집만 한다 — 업로드·다운로드 없음. 결과는 src/ledger.py 원장으로.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from typing import Any, Optional  # noqa: E402

from engine.common import get_logger, get_secret  # noqa: E402

log = get_logger("scout")

_API_BASE = "https://www.googleapis.com/youtube/v3"
_DUR_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


# ── 순수 헬퍼 ─────────────────────────────────────────────────────────────
def parse_iso8601_duration(s: str) -> Optional[float]:
    """'PT1M3S' → 63.0 (videos.list contentDetails.duration)."""
    m = _DUR_RE.match(s or "")
    if not m or not any(m.groups()):
        return None
    h, mi, sec = (int(g) if g else 0 for g in m.groups())
    return float(h * 3600 + mi * 60 + sec)


def shorts_playlist_id(channel_id: str) -> str:
    """UC<suffix> → UUSH<suffix> (Shorts 전용 자동 플레이리스트 — 미문서화 트릭.

    2026-07 실측으로 잔망루피 채널 Shorts 탭과 동일 목록 확인. 404 시 UU(전체)로 폴백."""
    if not channel_id.startswith("UC"):
        raise ValueError(f"채널 ID 형식 아님(UC...): {channel_id}")
    return "UUSH" + channel_id[2:]


def within_duration(duration: Optional[float], min_s: float, max_s: float) -> bool:
    """Shorts 길이 필터. 길이 미상(ytdlp flat)은 통과시키고 후단에서 재확인."""
    if duration is None:
        return True
    return min_s <= duration <= max_s


def parse_ytdlp_flat_lines(lines: list[str]) -> list[dict[str, Any]]:
    """yt-dlp --print "%(id)s\\t%(title)s\\t%(view_count)s" 출력 파싱."""
    rows = []
    for ln in lines:
        parts = ln.rstrip("\n").split("\t")
        if len(parts) < 3 or not parts[0]:
            continue
        vid, title, views = parts[0], parts[1], parts[2]
        try:
            vc: Optional[int] = int(float(views))
        except (TypeError, ValueError):
            vc = None
        rows.append({"video_id": vid, "title": title, "view_count": vc,
                     "like_count": None, "comment_count": None, "duration": None,
                     "published_at": None,
                     "url": f"https://www.youtube.com/shorts/{vid}"})
    return rows


# ── YouTube Data API ─────────────────────────────────────────────────────
def _api_get(endpoint: str, params: dict[str, Any], api_key: str) -> dict[str, Any]:
    q = dict(params, key=api_key)
    url = f"{_API_BASE}/{endpoint}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def resolve_channel_id(handle: str, api_key: str) -> str:
    data = _api_get("channels", {"part": "id", "forHandle": handle}, api_key)
    items = data.get("items", [])
    if not items:
        raise ValueError(f"채널을 찾을 수 없음: {handle}")
    return items[0]["id"]


def _list_playlist_video_ids(playlist_id: str, api_key: str, max_scan: int = 0) -> list[str]:
    ids: list[str] = []
    token = None
    while True:
        params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": 50}
        if token:
            params["pageToken"] = token
        data = _api_get("playlistItems", params, api_key)
        ids += [it["contentDetails"]["videoId"] for it in data.get("items", [])]
        token = data.get("nextPageToken")
        if not token or (max_scan and len(ids) >= max_scan):
            break
    return ids[:max_scan] if max_scan else ids


def _videos_stats(video_ids: list[str], api_key: str) -> list[dict[str, Any]]:
    rows = []
    for i in range(0, len(video_ids), 50):                    # videos.list 배치 상한 50
        batch = video_ids[i:i + 50]
        data = _api_get("videos", {"part": "snippet,contentDetails,statistics",
                                   "id": ",".join(batch)}, api_key)
        for it in data.get("items", []):
            st = it.get("statistics", {})
            rows.append({
                "video_id": it["id"],
                "title": it.get("snippet", {}).get("title", ""),
                "published_at": it.get("snippet", {}).get("publishedAt"),
                "duration": parse_iso8601_duration(
                    it.get("contentDetails", {}).get("duration", "")),
                "view_count": int(st["viewCount"]) if "viewCount" in st else None,
                "like_count": int(st["likeCount"]) if "likeCount" in st else None,
                "comment_count": int(st["commentCount"]) if "commentCount" in st else None,
                "url": f"https://www.youtube.com/shorts/{it['id']}",
            })
    return rows


def fetch_comment_texts(video_id: str, api_key: str, sample: int = 100) -> Optional[list[str]]:
    """댓글 텍스트 표본(언어 분석용). 댓글 비활성(403)·오류 시 None — 신호 없음."""
    try:
        data = _api_get("commentThreads", {"part": "snippet", "videoId": video_id,
                                           "maxResults": min(sample, 100),
                                           "textFormat": "plainText"}, api_key)
        return [it["snippet"]["topLevelComment"]["snippet"].get("textOriginal", "")
                for it in data.get("items", [])]
    except Exception as e:                                    # commentsDisabled 등
        log.info("댓글 수집 불가(%s): %s", video_id, e)
        return None


# ── yt-dlp 폴백 ──────────────────────────────────────────────────────────
def _scout_ytdlp(handle: str, max_scan: int = 0) -> list[dict[str, Any]]:
    url = f"https://www.youtube.com/{handle}/shorts"
    cmd = [sys.executable, "-m", "yt_dlp", "--flat-playlist", "--no-warnings",
           "--print", "%(id)s\t%(title)s\t%(view_count)s"]
    if max_scan:
        cmd += ["--playlist-end", str(max_scan)]
    res = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=600)
    if res.returncode != 0:
        raise RuntimeError(f"yt-dlp 실패: {res.stderr[-500:]}")
    return parse_ytdlp_flat_lines(res.stdout.splitlines())


# ── 진입점 ───────────────────────────────────────────────────────────────
def scout(config: dict[str, Any]) -> list[dict[str, Any]]:
    """채널 Shorts 열거 + 지표 → 원장 upsert 용 행 목록."""
    ap = config.get("autopilot", {})
    handle = ap.get("channel_handle", "@zanmangloopy")
    backend = str(ap.get("scout_backend", "auto")).lower()
    max_scan = int(ap.get("max_scan", 0))
    api_key = get_secret("YOUTUBE_API_KEY")

    if backend == "auto":
        backend = "api" if api_key else "ytdlp"
    if backend == "api" and not api_key:
        raise RuntimeError("scout_backend=api 이지만 YOUTUBE_API_KEY 미설정 (.env)")

    if backend == "api":
        cid = resolve_channel_id(handle, api_key)
        try:
            vids = _list_playlist_video_ids(shorts_playlist_id(cid), api_key, max_scan)
            log.info("UUSH Shorts 플레이리스트로 %d편 열거", len(vids))
        except Exception as e:                                # UUSH 미지원 채널 폴백
            log.warning("UUSH 실패(%s) → 전체 업로드(UU)에서 길이로 필터", e)
            vids = _list_playlist_video_ids("UU" + cid[2:], api_key, max_scan)
        rows = _videos_stats(vids, api_key)
    else:
        rows = _scout_ytdlp(handle, max_scan)

    lo, hi = float(ap.get("min_duration", 3)), float(ap.get("max_duration", 183))
    kept = [r for r in rows if within_duration(r.get("duration"), lo, hi)]
    log.info("스카우트 완료(backend=%s): %d편 (길이 필터 후 %d)", backend, len(rows), len(kept))
    return kept
