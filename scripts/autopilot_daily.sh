#!/bin/bash
# loopy-jp autopilot 일일 실행 (launchd 가 호출). caffeinate 로 실행 중 슬립 방지.
set -u
# launchd 는 최소 PATH 로 실행 — ffmpeg/ffprobe(Homebrew) 탐색용 보강.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
# 레포 경로는 스크립트 위치에서 유도 — 머신/계정 바뀌어도 plist 만 맞으면 동작.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$REPO/outputs/autopilot_daily.log"
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') daily 시작 ====="
  cd "$REPO" || exit 1
  /usr/bin/caffeinate -i "$REPO/.venv/bin/python" -m src.autopilot daily
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') daily 종료 (exit $?) ====="
} >> "$LOG" 2>&1
