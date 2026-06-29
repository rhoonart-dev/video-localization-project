#!/usr/bin/env bash
# 모델 가중치 다운로드 (자리만 — 각 항목은 라이선스 확인 후 주석 해제).
# ⚠ 상업 채널: 모델/가중치 라이선스를 확인하기 전에는 다운로드/사용하지 말 것.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "weights/ → ${HERE}"

# --- LaMa (Apache-2.0) — simple-lama-inpainting 이 최초 실행 시 자동 캐시 ---
# pip install simple-lama-inpainting  # 가중치는 라이브러리가 받아옴
# TODO(라이선스): 모델 가중치 배포 라이선스 최종 확인.

# --- STTN ---
# TODO(라이선스): STTN 저장소/가중치 라이선스 확인.
# mkdir -p "${HERE}/sttn"
# curl -L -o "${HERE}/sttn/sttn.pth" "<WEIGHT_URL>"

# --- ProPainter ---
# ⛔ TODO(라이선스): S-Lab 비상업 가능성. 상업 사용 가능 확인 전 금지.
#    확인 후 config inpaint.propainter_commercial_ack=true 로 명시.
# mkdir -p "${HERE}/propainter"
# curl -L -o "${HERE}/propainter/ProPainter.pth" "<WEIGHT_URL>"

echo "현재 활성 다운로드 없음 — 라이선스 확인 후 해당 블록 주석 해제."
