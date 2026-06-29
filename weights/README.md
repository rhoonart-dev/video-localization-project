# weights/ — 모델 가중치 (git 무시)

인페인팅 백엔드 가중치를 여기에 배치한다. **상업 채널이므로 빌드 전 라이선스 확정 필수.**

| 백엔드 | 경로 | 라이선스 | 상업 사용 |
|---|---|---|---|
| LaMa (`simple-lama-inpainting`) | 자동 캐시(최초 실행 시 다운로드) | Apache-2.0 (모델 가중치 별도 확인) | ✅ 가장 안전 |
| STTN | `weights/sttn/` | 저장소·가중치별 상이 | ⚠ 확인 필요 |
| ProPainter | `weights/propainter/` | **S-Lab 등 비상업 가능성** | ⛔ 확인 전 사용 금지 |
| OpenCV (Telea) | 불필요 | OpenCV(Apache-2.0) | ✅ (품질 낮음, 검증용 폴백) |

## 사용 가드
- `opencv` 백엔드는 가중치 없이 동작 → 파이프라인 검증·스모크 테스트용.
- `propainter` 는 `config.inpaint.propainter_commercial_ack=true` 로 명시하지 않으면 코드가 차단한다.
- 가중치 다운로드/배치 후, 각 백엔드 어댑터(`engine/inpaint.py` 의 STTN/ProPainter)에서
  실제 모델 로드를 연결해야 한다(현재는 라이선스 확인 TODO 와 함께 미연동 stub).

`download.sh` 참고 — 다운로드 자리만 있고, 각 항목은 **라이선스 확인 TODO** 로 막혀 있다.
