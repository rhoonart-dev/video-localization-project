# 검수 체크리스트 & 업로드 SOP — loopy-jp

> 파이프라인은 **초벌(드래프트)** 까지만 만든다. 게시·삭제·권한변경 등 비가역 동작은 **사람이** 한다.
> 자동 게시 금지(`config.upload.auto_publish=false` 고정).

---

## A. 검수 체크리스트 (영상 1편)

### 게이트① 번역·트랜스크리에이션 (네이티브)
- [ ] `translations.json` 의 일본어가 직역이 아닌 트랜스크리에이션인가
- [ ] 캐릭터 **어미 일관성** — persona 채택 어미가 전 구간 동일하게 적용됐는가
- [ ] 어미가 정보성 텍스트(설명/©)에 잘못 들어가지 않았는가
- [ ] `glossary.yaml` 고정 표기(음식명·고유명사)가 지켜졌는가
- [ ] `flagged=true` 항목 전부 확인했는가
- [ ] 민감 소재·캐릭터 이탈 표현 없는가

### 게이트② 인페인팅 품질 (PSNR/SSIM 자동 + 육안)
- [ ] `review_report.md` 의 평균 PSNR/SSIM 확인 (프록시 점수임을 이해)
- [ ] **플래그된 타임코드 전부 육안 확인** (경계 잔상·번짐·깜빡임)
- [ ] `qa/cmp_*.png` side-by-side 로 아티팩트 확인
- [ ] 고움직임·복잡 배경 구간 별도 확인
- [ ] (필요 시) ROI 재지정/백엔드 변경 후 재처리

### 게이트③ 더빙 (Level C 한정)
- [ ] `dub_ja_draft.wav` / `final_dubbed.mp4` 클론 보이스가 캐릭터에 맞는가(또는 보이스 디렉션 일관)
- [ ] 자막/대사 타이밍과 음성 싱크
- [ ] 원음(ASMR 등)과 더빙 믹스 밸런스(`dub.bg_volume`) 적절한가
- [ ] retention 리스크 — hero 영상은 성우 검토
- [ ] (상업) XTTS-v2 가중치는 **비상업 라이선스** — 게시본은 상업가능 보이스(ElevenLabs 등)로 교체했는가

### 공통 산출물
- [ ] 자막 싱크: `ja.ass` / `ja.srt` 화면과 일치
- [ ] 썸네일: `thumb_ja_v*.png` 캐릭터 비주얼 유지, 카피 적절
- [ ] 메타데이터: `metadata_draft.json` 제목/설명/해시태그/태그/© 라인
- [ ] © 라인 표기 (필요 시): `© ICONIX / OCON / EBS / SKbroadband`
- [ ] 업로드 시간(JST) 설정
- [ ] 고정 댓글·커뮤니티 포스트(JP) 준비

---

## B. 업로드 SOP (단계별 산출물·책임자)

| 단계 | 명령 | 산출물 | 책임자 |
|---|---|---|---|
| 1. 선별 | `python -m src.select --analytics ...` | `outputs/selection_ranked.csv` | 운영 |
| 2. 메타 | `python -m src.metadata --video-id ID --title ...` | `metadata_draft.json` | 운영 |
| 3. 가공(엔진①②③) | `python -m src.process_video --video ... --level B` | `final_draft.mp4`, `ja.ass/srt`, `review_report.md` | 엔지니어 |
| → 게이트① 번역 검수 | (사람) | 승인/수정 | 네이티브 검수자 |
| → 게이트② 인페인팅 검수 | (사람) | 승인/재처리 | 엔지니어+검수자 |
| 4. 썸네일 | `python -m src.thumbnail --video-id ID --base ...` | `thumb_ja_v*.png` | 디자이너 |
| → 게이트③ 더빙(C) | `python -m src.dub --video-id ID --video dialogue.mp4 --backend xtts --speaker loopy.wav` | `dub_ja_draft.wav`, `final_dubbed.mp4` | 성우/검수자 |
| 5. 업로드 | **사람이 YouTube Studio 에서 최종 클릭** | 게시 | 운영 |
| 6. 측정·튜닝 | 분석 CSV → `src.select` 재투입 | 다음 배치 우선순위 | 운영 |

### 금지 사항 (명시)
- ❌ 파이프라인의 **자동 게시/삭제/권한변경**
- ❌ 원본 영상 덮어쓰기 (산출물은 `outputs/{video_id}/` 별도)
- ❌ 게이트 미통과 본 게시
- ❌ ProPainter 등 **상업 라이선스 미확인** 모델로 매출 영상 처리

### 측정 루프 (플라이휠)
업로드 후 KPI(retention·CTR·일본 시청 비중·구독 전환·일본어 댓글 비율)를 분석 CSV 로 모아
`src.select` 입력으로 재투입 → 다음 배치 우선순위 갱신.
