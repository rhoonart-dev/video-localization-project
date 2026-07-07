#!/usr/bin/env python3
"""YouTube API 업로드 잠금 실측 테스트 (진단용 — 파이프라인 아님).

목적: "감사(audit) 미통과 프로젝트로 videos.insert 하면 영상이 private 으로 잠긴다"
(2020-07-28 정책)가 우리 프로젝트에 실제로 적용되는지 실측.

  1) OAuth 디바이스 플로우 인증(브라우저에서 코드 입력 — 테스트 채널 계정 권장)
  2) 중립 테스트 클립(컬러바 2초)을 privacyStatus=private 로 업로드
  3) status 조회 → videos.update 로 public 전환 시도
  4) 판정: 전환 성공 = 잠금 없음(자동 업로드 가능!) / 실패·잠김 = 감사 필요 확정

필요 시크릿(.env): YT_OAUTH_CLIENT_ID / YT_OAUTH_CLIENT_SECRET
  (GCP 콘솔 → 사용자 인증 정보 → OAuth 클라이언트 ID → 유형 "TV 및 제한된 입력 장치")
토큰 캐시: outputs/yt_oauth_token.json (gitignore 됨). 실행: python scripts/yt_upload_test.py
⚠ 테스트 영상은 잠기면 영구 비공개 — 본채널이 아닌 테스트 채널 계정으로 인증할 것.
업로드 후 필요 없으면 Studio 에서 삭제 가능.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from engine.common import ensure_dir, get_secret, resolve_path  # noqa: E402

SCOPE = "https://www.googleapis.com/auth/youtube"   # upload + update(공개 전환 시도)
TOKEN_CACHE = resolve_path("outputs/yt_oauth_token.json")


def _post(url: str, data: dict, headers: dict | None = None) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def device_flow(client_id: str, client_secret: str) -> dict:
    """OAuth 디바이스 플로우 → 토큰 dict. 사람이 브라우저에서 코드 1회 입력."""
    d = _post("https://oauth2.googleapis.com/device/code",
              {"client_id": client_id, "scope": SCOPE})
    if "verification_url" not in d:
        raise SystemExit(f"디바이스 코드 발급 실패: {d} — OAuth 클라이언트 유형이 "
                         f"'TV 및 제한된 입력 장치'인지 확인")
    print(f"\n▶ 브라우저에서 열기: {d['verification_url']}")
    print(f"▶ 코드 입력: {d['user_code']}")
    print("  (⚠ 본채널 말고 테스트 채널 계정으로 로그인 권장)\n대기 중...")
    while True:
        time.sleep(int(d.get("interval", 5)))
        tok = _post("https://oauth2.googleapis.com/token",
                    {"client_id": client_id, "client_secret": client_secret,
                     "device_code": d["device_code"],
                     "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if "access_token" in tok:
            return tok
        if tok.get("error") not in ("authorization_pending", "slow_down"):
            raise SystemExit(f"인증 실패: {tok}")


def get_token() -> str:
    cid = get_secret("YT_OAUTH_CLIENT_ID")
    csec = get_secret("YT_OAUTH_CLIENT_SECRET")
    if not (cid and csec):
        raise SystemExit(
            "OAuth 클라이언트 미설정. GCP 콘솔에서 2분 작업 필요:\n"
            "  1) console.cloud.google.com → API 및 서비스 → 사용자 인증 정보\n"
            "  2) + 사용자 인증 정보 만들기 → OAuth 클라이언트 ID\n"
            "     (동의 화면이 없다면 먼저 만들라고 안내됨 — 외부/테스트 모드, 본인 계정을 테스트 사용자로)\n"
            "  3) 애플리케이션 유형: 'TV 및 제한된 입력 장치'\n"
            "  4) 생성된 클라이언트 ID/보안 비밀을 .env 에:\n"
            "     YT_OAUTH_CLIENT_ID=...\n     YT_OAUTH_CLIENT_SECRET=...")
    if TOKEN_CACHE.exists():
        cache = json.loads(TOKEN_CACHE.read_text())
        tok = _post("https://oauth2.googleapis.com/token",
                    {"client_id": cid, "client_secret": csec,
                     "refresh_token": cache["refresh_token"],
                     "grant_type": "refresh_token"})
        if "access_token" in tok:
            return tok["access_token"]
        print("리프레시 실패 → 재인증")
    tok = device_flow(cid, csec)
    ensure_dir(TOKEN_CACHE.parent)
    TOKEN_CACHE.write_text(json.dumps({"refresh_token": tok["refresh_token"]}))
    return tok["access_token"]


def make_test_clip() -> pathlib.Path:
    """중립 테스트 클립(컬러바+톤 2초, 세로) — IP 콘텐츠를 테스트에 쓰지 않는다."""
    out = resolve_path("outputs/upload_test.mp4")
    if not out.exists():
        subprocess.run(["ffmpeg", "-y", "-v", "error",
                        "-f", "lavfi", "-i", "smptebars=size=720x1280:rate=24",
                        "-f", "lavfi", "-i", "sine=frequency=440",
                        "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", str(out)], check=True)
    return out


def api(method: str, url: str, token: str, body: bytes | None = None,
        content_type: str = "application/json") -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def upload(token: str, video: pathlib.Path) -> str:
    meta = {"snippet": {"title": f"loopy-jp API upload test {uuid.uuid4().hex[:6]}",
                        "description": "감사(audit) 잠금 실측용 테스트 — 삭제 예정",
                        "categoryId": "22"},
            "status": {"privacyStatus": "private",
                       "selfDeclaredMadeForKids": False}}
    boundary = f"b{uuid.uuid4().hex}"
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(meta)}\r\n--{boundary}\r\nContent-Type: video/mp4\r\n\r\n"
            ).encode() + video.read_bytes() + f"\r\n--{boundary}--".encode()
    code, res = api("POST",
                    "https://www.googleapis.com/upload/youtube/v3/videos"
                    "?part=snippet,status&uploadType=multipart",
                    token, body, f"multipart/related; boundary={boundary}")
    if code != 200:
        raise SystemExit(f"업로드 실패 HTTP {code}: {json.dumps(res, ensure_ascii=False)[:500]}")
    return res["id"]


def main() -> None:
    token = get_token()
    clip = make_test_clip()
    print(f"테스트 클립: {clip}")
    vid = upload(token, clip)
    print(f"✅ 업로드 성공: https://youtube.com/watch?v={vid} (private 로 업로드됨)")

    _, res = api("GET", f"https://www.googleapis.com/youtube/v3/videos?part=status&id={vid}", token)
    st = (res.get("items") or [{}])[0].get("status", {})
    print(f"업로드 직후 status: {json.dumps(st, ensure_ascii=False)}")

    print("\n▶ 핵심 실험: public 전환 시도...")
    body = json.dumps({"id": vid, "status": {"privacyStatus": "public",
                                             "selfDeclaredMadeForKids": False}}).encode()
    code, res = api("PUT", "https://www.googleapis.com/youtube/v3/videos?part=status", token, body)
    if code == 200 and res.get("status", {}).get("privacyStatus") == "public":
        print("\n🟢 결론: public 전환 성공 — 이 프로젝트는 잠금 없이 업로드 가능!")
        print("   (다시 private 로 되돌립니다)")
        body = json.dumps({"id": vid, "status": {"privacyStatus": "private",
                                                 "selfDeclaredMadeForKids": False}}).encode()
        api("PUT", "https://www.googleapis.com/youtube/v3/videos?part=status", token, body)
    else:
        print(f"\n🔴 결론: public 전환 실패/거부 (HTTP {code})")
        print(f"   응답: {json.dumps(res, ensure_ascii=False)[:400]}")
        print("   → 감사(audit) 필요 확정. Studio 에서도 해당 영상의 공개 설정이 잠겨 있는지 확인.")
    print(f"\n테스트 영상은 Studio 에서 삭제 가능: https://studio.youtube.com")


if __name__ == "__main__":
    main()
