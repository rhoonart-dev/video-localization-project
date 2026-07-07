#!/bin/bash
# loopy-jp autopilot 일일 실행 (launchd 가 호출). caffeinate 로 실행 중 슬립 방지.
set -u
REPO="/Users/gimsewon/rhoonart/video-localization-project"
LOG="$REPO/outputs/autopilot_daily.log"
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') daily 시작 ====="
  cd "$REPO" || exit 1
  /usr/bin/caffeinate -i "$REPO/.venv/bin/python" -m src.autopilot daily
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') daily 종료 (exit $?) ====="
} >> "$LOG" 2>&1
