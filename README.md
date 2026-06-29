# loopy-jp — 잔망루피 일본 채널 영상 현지화 엔진

한국 잔망루피 영상을 일본 전용 채널용으로 가공하는 **자체 현지화 파이프라인**.
외부 SaaS(GhostCut) 없이 **OCR → 인페인팅 → 트랜스크리에이션 → 원본 스타일 재합성**을 직접 조립한다.

> 자체 구축의 가치 포인트는 ③(번역 + 원본 스타일 재렌더). ①②(OCR·인페인팅)는 검증된
> 오픈소스를 *라이브러리로* 쓰고, 차별화·품질 관리는 ③ + QA·배치 오케스트레이션에 집중한다.

---

## ⚠ 먼저 읽기 (상업 채널 가드)

1. **모델 라이선스 확인 필수.** LaMa(Apache-2.0)·PaddleOCR(Apache-2.0)은 상업 가능 범주로
   알려졌으나, **ProPainter 는 S-Lab 등 비상업/연구용 라이선스일 수 있다.** 매출과 직결되므로
   상업 사용 전 저장소·가중치 라이선스를 확정하라. (`weights/README.md`, 법무 확인 권장)
   - 코드 가드: `propainter` 는 `config.inpaint.propainter_commercial_ack=true` 없으면 차단.
2. **비가역 동작 자동화 금지.** 게시·삭제·권한변경은 파이프라인이 하지 않는다.
   YouTube 업로드는 **사람이 최종 클릭**. `config.upload.auto_publish` 는 항상 `false`.
3. **모든 산출물은 초벌(드래프트).** 번역·인페인팅·더빙은 검수 게이트(①②③) 통과 후에만 게시.
4. **폰트 라이선스**(임베딩/방송)도 확인 — `fonts/README.md`.

---

## 구조

```
loopy-jp/
  config/   pipeline.config.yaml · persona.md · font_map.yaml · glossary.yaml
  engine/   detect → mask → inpaint → translate → render → qa   (+ common, schemas)
  src/      process_video(오케스트레이터) · select · metadata · thumbnail · dub
  data/     analytics/(KPI CSV)  source/(원본)
  outputs/{video_id}/   detections.json · masks/ · inpainted/ · translations.json
                        · ja.ass/ja.srt · rendered/ · final_draft.mp4 · review_report.md
  weights/  인페인팅 가중치(git 무시, 라이선스 확인)
  fonts/    일본어 폰트(git 무시, 라이선스 확인)
  tests/    순수 로직 단위 테스트
```

데이터 계약(detections.json / translations.json)은 `engine/schemas.py` 의 dataclass 로 통일.

## 설치

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # core. OCR/인페인팅 백엔드는 주석 참고해 추가
cp .env.example .env                      # LLM_API_KEY 등 채우기 (코드 하드코딩 금지)
# 시스템 의존: ffmpeg / ffprobe, (인페인팅) NVIDIA CUDA GPU 권장
```

인페인팅 백엔드:
- `opencv` — 가중치 불필요, 무설치 폴백(품질 낮음, 파이프라인 검증용·기본값).
- `lama` — `pip install simple-lama-inpainting` (Apache-2.0, 권장).
- `sttn` / `propainter` — 가중치 배치 + 어댑터 연동 필요(`weights/`, 라이선스 확인).

## 실행 순서 (영상 1편)

```bash
# 1) 선별 — 일본 진출 우선순위·등급(추정)
python -m src.select --analytics data/analytics/sample_analytics.csv

# 2) 메타데이터 초벌 (제목/설명/태그/해시태그, JP)
python -m src.metadata --video-id vid001 --title "불닭 라면 먹방 ASMR"

# 3) 영상 가공 (엔진①②③ + QA). Level A=자막중심 / B=번인재편집 / C=더빙포함
python -m src.process_video --video data/source/vid001.mp4 --video-id vid001 \
    --level B --content-type mukbang --backend lama
#   → outputs/vid001/{final_draft.mp4, ja.ass, ja.srt, translations.json, review_report.md}

# 4) 썸네일 (일본어 카피)
python -m src.thumbnail --video-id vid001 --base data/source/vid001_thumb.png \
    --title "불닭 라면 먹방 ASMR"

# 5) (Level C 한정, 게이트 통과 후) 더빙 — 오픈소스 보이스 클로닝(XTTS, 기본)
#   자막 기반:
python -m src.dub --video-id vid001 --subtitle outputs/vid001/ja.srt --level C \
    --backend xtts --speaker voices/loopy_sample.wav
#   대사 영상 풀 플로우(ASR 받아쓰기 → 트랜스크리에이션 → 클론 합성 → 원음 믹스):
python -m src.dub --video-id vid001 --video data/source/vid001_dialogue.mp4 --level C \
    --backend xtts --speaker voices/loopy_sample.wav
#   원본 목소리 제거(깨끗한 더빙): config dub.remove_original_vocals=true (Demucs 보컬 분리).
#   합성 음성은 자막 슬롯 길이에 맞춰 time-stretch(싱크) + 라우드니스 정규화(-16 LUFS) 자동 적용.
#   ⚠ XTTS-v2 가중치 = 비상업 라이선스. 상업 게시엔 --backend elevenlabs --voice <ID> 등 상업가능 옵션.
```

→ `review/checklist.md` 의 게이트①②③ 검수 후 **사람이 업로드**. 6) KPI 를 다시 `src.select` 로.

개별 엔진 단계도 따로 실행 가능: `python -m engine.detect --help`, `engine.inpaint`, `engine.render` 등.

## 테스트

```bash
python tests/run_all.py        # pytest 미설치 환경용 러너(stdlib)
pytest -q                      # pytest 설치 시
```
순수 로직(스키마·마스크 기하·선별 점수·QA 플래그·렌더 자막·번역 프롬프트 등)을 의존성 없이 검증한다.
무거운 경로(OCR·인페인팅·LLM)는 lazy import 로 분리되어, 미설치 환경에서도 import/CLI 가 동작한다.

## 라이선스·운영 메모
- 라이선스 확보 완료 전제(© ICONIX / OCON / EBS / SKbroadband — 필요 시 설명란).
- GPU 비용·품질을 파일럿(10편)에서 SaaS 와 비교 후 확장 판단(자체 구축이 항상 싸지 않다).
- 캐릭터 어미(한국 "~뤂" 대응)는 `config/persona.md` 에서 1개 확정 후 전 영상 일관 적용.
