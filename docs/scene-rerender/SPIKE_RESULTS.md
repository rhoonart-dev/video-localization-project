# Phase 0 스파이크 결과 — 2026-08-04

> 대상: `혜미리예채파_74` (ショトコン EP1, 미공개 대기분) 복사본 → `spike/혜미리예채파_74/`
> 산출물: 일본어판 `spike/혜미리예채파_74/shorts.mp4` (49.749s — 원본과 프레임 단위 동일 컷)
> 원본(한국어판)은 `shorts_ko_original.mp4` 로 보존. 검증 프레임은 `frames/`.

## 결론: **후처리 재렌더 방식 성립. 전 항목 통과.** Phase 1 진행 가능.

| §8 항목 | 결과 |
|---|---|
| 1. 세그먼트 교체 → 일본어 번인 | ✅ `subtitle_segments.json` 교체 후 `--from-step render` 재렌더로 일본어 자막 번인 확인 |
| 2. `--title` 밴드 반영·부작용 | ✅ 밴드만 ヘミリイェチェパ 로. `--job-id` 지정 시 디렉토리 재사용 확인. ⚠️ `work_title.txt` 가 일본어로 덮임 — **소비자 없음 확인**(brain·ai-video 전체 grep — write-only 기록 파일)이라 무해하나 구현에선 원복 |
| 3. 일본어 폰트 | ✅ 제목·밴드: `--design-title-font ArialUnicode`(스파이크용 macOS Arial Unicode 복사본, ai-video `app/assets/fonts/` untracked). 자막·TTS: 프리셋 폰트(Jalnan Gothic, 한글 전용)임에도 libass 시스템 폴백으로 **두부 없이** 렌더됨. 단 폴백 서체는 통제 불가 |
| 4. TTS 자막 일본어화 | ✅ `checkpoint_resources.json`의 `tts_cue_files[].cue.text` 교체로 하늘색 TTS 자막 일본어 렌더. 한국어 TTS 음성과의 병행은 파일럿에서 사람 검수 |
| 5. 텔롭 추출 | ✅ Gemini 3.1 Pro에 완성 쇼츠(33MB) 입력 → **61초, 토큰 in 4.8k/out 1.5k**로 방송 텔롭 16건을 시간·위치·종류와 함께 추출+일본어 번역(`spike/onscreen_extract_result.json`). 우리 자막/제목은 스스로 분류 제외. 사투리("미안합니데이"→"すんまへん")도 처리 |
| 6. provenance/인제스트 | ✅ `ingest_aivideo_run.py` 는 `(ai_video_run_id, short_label)` 멱등 키 — 원본 run 디렉토리 안에서 재렌더하면 같은 클립 레코드로 갱신됨 |

## 스파이크가 밝혀낸 설계 수정 사항 (PLAN 대비 변경)

1. **상단 제목의 정본은 `edit_plan.json`이 아니라 `checkpoint_story.json`** — 재개 시
   `variants[].title_text` 에서 제목을 다시 읽고(edit_plan 은 story 체크포인트가 없을 때의
   폴백일 뿐, `pipeline.py:2441-2466`), edit_plan 수정만으로는 반영되지 않는다(1차 재렌더에서 실증).
   → 현지화는 **둘 다** 수정한다(story=렌더 입력, edit_plan=발행 시 DB 제목 조회용).
2. **재렌더는 원본 생성 플래그를 그대로 반복해야 한다** — 1차 재렌더에서 silence 프로파일이
   기본값(conservative)으로 떨어져 **컷이 49.7s→53.3s 로 달라졌다**(자막 싱크 전부 어긋남).
   `loop_policy.json gen_flags_base`(`--silence-profile aggressive --length-profile tight
   --loudness-lufs -14`)를 붙이자 컷이 프레임 단위로 재현됐다(49.749333s 동일).
   → 구현에서는 run_log/loop_policy 에서 플래그를 읽어 자동으로 재현한다.
3. **원본 ショトコン 쇼츠에는 대사 자막이 애초에 없었다** — works.json `subtitles: "none"` →
   scene_loop 가 `--no-subtitles` 로 생성(오자막 방지 합의, 2026-07-29). 일본어판은 자막을
   **켜는** 것이므로 이 정책의 예외가 된다. 오자막 리스크가 되살아나므로 완화가 필수:
   - 실증 사례: Whisper 가 "멀티 그루**브**"를 "멀티 그룹"으로 오청취 — **텔롭 추출(L2)이
     "멀티 그루브거든"을 정확히 읽어** 교정 단서를 제공했다. → L1 번역 프롬프트에 L2 텔롭
     원문을 함께 넣어 대사를 교정-번역하게 한다(순서: L2 → L1).
   - 22초짜리 비정상 세그먼트(환각 의심)도 있었다 → L1 에서 길이 이상 세그먼트 검사.
4. **자막 폰트는 CLI 로 못 바꾼다** — `--design-title-font` 는 있지만 자막·TTS 폰트 플래그가
   없다(장르 프리셋 고정). 스파이크에선 시스템 폴백으로 우연히 잘 나왔지만 서체 통제가 안 되므로,
   Phase 1 에서 ai-video 에 `--design-subtitle-font` 플래그 추가(소규모, 기존 design 계층에 1개 추가)
   또는 일본어 프리셋 신설이 필요하다. 폰트 자체도 정식 선정 필요(잘난체 대체 — 라이선스 확인 후 다운로드는 사용자 승인).
5. **일본어 줄바꿈은 공백 기반**(`subtitle.py:14 text.split()`) — 번역 시 구절 경계에
   전각/반각 공백을 넣도록 프롬프트에 강제하면 현행 로직으로 접힌다(스파이크에서 2줄 래핑 확인).
   장기적으로 budoux 등 일본어 분절 도입 검토.
6. **이미 unlisted 로 업로드된 대기 3편** — 유튜브는 영상 파일 교체가 불가하므로 현지화본은
   **새 업로드**가 되고, 기존 unlisted 는 삭제해야 한다(삭제는 되돌릴 수 없어 사람 결정 사항).

## 비용/시간 실측

| 작업 | 실측 |
|---|---|
| 재렌더(캐시 콜드 — silence 재계산 포함) | 7.3분 |
| 재렌더(캐시 웜) | **51.5초** (ffmpeg 자체 6.2초) |
| 텔롭 추출+번역 (Gemini 3.1 Pro) | 61초, ~6.3k tokens |
| 대사 번역 (스파이크는 수동 — 구현 시 Flash 1콜) | 예상 수 초 |

편당 총 현지화 오버헤드 예상: **약 2~3분 + Gemini 호출 2회** (생성 68분 대비 미미).

## 스파이크 잔여물

- `spike/혜미리예채파_74/` — 일본어판 shorts.mp4 + 교체된 데이터 파일들(원본 백업은 원 job 디렉토리가 그 자체)
- `spike/extract_onscreen_spike.py`, `spike/onscreen_extract_result.json`
- ai-video `app/assets/fonts/ArialUnicode.ttf` (untracked) — 정식 폰트 선정 전 임시. 커밋 금지
