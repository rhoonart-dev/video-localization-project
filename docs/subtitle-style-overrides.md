# 일본어 자막 줄 스타일·타이밍 오버라이드 — 엔진 계약 (JP-2, 2026-08-20)

검수함 반려-수정 재렌더(8/14)의 오버라이드에 **줄 단위 스타일과 타이밍**을 싣는 계약.
의미는 ai-video `edit_overrides/v3` 의 `subtitles[].style`(F-407/F-410)과 동일하다 —
편집실(ves-orchestrator) WYSIWYG 이 두 경로(SHOTCONE·잔망루피)에서 같은 값으로 같은
화면을 얻게 하기 위해서다. 검증·ASS 태그 조립의 정본은 `engine/render.py`
(`validate_line_style` · `validate_line_timing` · `style_ass_tags` · `style_margin_v`)
하나이고 두 경로가 공유한다.

## 오버라이드 값 스키마 (두 경로 공통)

subs / telops 의 dict 값에 선택 키가 추가된다(종전 `"ja"` 문자열/dict 규약 그대로):

```json
{ "ja": "…", "style": { "size": 64, "y": 0.8, "color": "#FFDD00", "rotate": -8 },
  "start_sec": 12.4, "end_sec": 15.0 }
```

| 키 | 의미 | 범위/형식 | 검증 위반 시 |
|----|------|-----------|--------------|
| `style.size` | 폰트 크기 | 양수. **1080×1920 캔버스 px** — 렌더 캔버스(PlayResY)가 다르면 엔진이 비율 환산 | ValueError |
| `style.y` | 줄 **하단**이 놓일 세로 위치 | 0(상단)~1(하단), 캔버스 비율. 하단 정렬 스타일에선 이벤트 MarginV=(1−y)×PlayResY(최소 1), 그 외 \pos 폴백 | ValueError |
| `style.color` | 글자색 | `"#RRGGBB"` → ASS `\1c&HBBGGRR&` | ValueError |
| `style.rotate` | 줄 회전 | -180~180 도, **시계방향 양수**(v3 images 와 동일 규약). ASS `\frz` 는 반시계 양수 — **부호 반전은 엔진(style_ass_tags) 책임**, 편집실은 계약 부호만 보낸다. 0 은 태그를 안 박는다 | ValueError |
| `start_sec` / `end_sec` | 표시 구간 | 초, **편집본(영상) 시간축**, ≥0, 둘 다 있으면 end > start | ValueError |
| `use` (E6-0) | **소프트 삭제** — `false` = 그 줄을 렌더에서 뺀다(번인·사이드카 ass/srt·다음 카드 pairs 전부). 삭제가 같은 항목의 다른 diff 를 이긴다(편집실이 `{use:false}` 만 보낸다) | 불리언. 텔롭은 0038 원계약 그대로 | ValueError(불리언 외) |

- **모르는 style 키는 즉시 거절**(조용한 무시 = 사람이 고친 값 증발 — v3 과 동일 원칙).
- 명시한 키만 얹는다 — `{"style": {"color": "#FF0000"}}` 이면 크기·위치는 기존 그대로.
- **tts 항목의 style·start_sec/end_sec·use 는 이번 판 범위 밖 — 즉시 거절한다(후속)**.
  근거: ai-video 계약이 TTS cue 단위 스타일을 받지 않고(디자인 레벨은 KR 와 공유),
  tts 타이밍·삭제 편집은 재합성 창(fit) 재계산이 얽힌다. 조용한 무시 대신 fail-loud.
- **subs use=false 의 경로별 의미(E6-0)**: SHOTCONE 은 l3_apply 가 segments 전사에서
  그 줄을 뺀다(ai-video 렌더 제외). 잔망루피 C/BC 는 빈 대사 필터와 같은 지점에서
  이벤트를 빼 **TTS 합성·자막(srt/ass)·retime 이 함께 빠지고**, 시작 시각은 아무도
  옮기지 않으므로 그 창은 무음으로 남는다(뒤 이벤트가 당겨오지 않는다). BJ/B 는
  `TranslationDoc.as_map` 이 tmap 에서 빼 번인(replace)·ass/srt·ja_events 에서 빠진다.
  다음 카드 pairs(build_ko_ja_pairs·build_dub_pairs)에서도 빠진다 — 좌표(idx)는 필터
  전 순번이라 남은 줄의 idx 는 그대로다.

## 경로별 소비

### SHOTCONE (scene_rerender — scripts/localize_run.py)

- 좌표: 종전 그대로 `--overrides` JSON 의 `subs{idx}`/`telops{idx}` = translation 각
  목록의 `index`(= 검수 카드 ko_ja_pairs 의 idx).
- **대사(subs)**: L3 가 style·start_sec/end_sec 를 `subtitle_segments.json` 에 전사한다.
  ai-video(69e5c06, v3 캐시 규약 'style 은 남는다')가 `--from-step render` 재렌더에서
  그 파일의 줄 style 을 그대로 렌더에 소비한다 — 렌더 쪽 추가 작업 없음(실측 확인).
  **타이밍 우선순위**: 사용자 지정 start/end 가 있으면 8s/20자 ASR 환각 클램프를
  건너뛴다(사람이 보고 정한 값이 이긴다).
- **텔롭(telops)**: `build_telop_ass` 가 줄별 인라인 태그(`\fs`·`\1c`·`\frz` 부호 반전)
  + y→이벤트 MarginV(PlayResY 1920 환산)로 굽고, start/end 는 L2b 재보정 값보다
  사용자 지정이 우선. 태그는 `_ass_escape` 밖에서 조립한다({ } 이스케이프와 충돌 방지).

### 잔망루피 C/BC (더빙 번인 — src/dub.py)

- 좌표: 종전 그대로 `outputs/{video_id}/overrides.json` 의 `subs{idx}` = ko_ja_pairs
  subs 의 idx(ASR 세그 순번, 빈 대사 필터 **전**).
- `apply_dub_overrides` 가 합성 **전**(ja_dub.srt 1차 기록 전)에 style·타이밍을 events 에
  병합한다 → 사용자 타이밍이 페이싱 캡(`segment_hard_caps`)·합성 슬롯에 그대로 반영된다.
- **retime 우선순위 규칙(신설)**: `end_sec` 를 지정한 세그는 `end_fixed` 로 표시되고,
  `retime_events`(실측 더빙 길이 재정렬)가 **덮어쓰지 않는다** — 사용자 값 우선.
  start 이동으로 순서가 바뀌면 시작 시각 오름차순으로 재정렬 후 진행한다.
- style 은 `ja_dub.ass`(`engine/render.build_ass`) 이벤트별 태그로 반영된다. size 는
  1080×1920 기준 px 를 실제 영상 높이에 비례 환산.
- C 루트 병합 실패 정책은 종전 그대로: 예외 시 경고 로그 후 원문 진행(더빙을 죽이지
  않는다) — 검증 위반이면 해당 재렌더의 오버라이드 전체가 무시되므로 재검수에서 걸러진다.

### 잔망루피 BJ/B (병기·자막 — src/process_video.py + engine/render.py)

- 좌표: 종전 그대로 `overrides.json` 의 `subs{idx}` = `translations.json` entries 순번.
- `_apply_subtitle_overrides` 가 ja 외에 style·start_sec/end_sec 를 검증 후
  entries[idx] 에 저장하고(위반 = 즉시 실패), `render.attach_entry_overrides` 가
  source 텍스트 매칭으로 이벤트에 전사한다. 같은 원문이 여러 시간 구간에 등장하면
  style 은 전부에, 타이밍은 **첫 이벤트에만** 적용(중복 배치 방지) + 경고 로그.
- BJ 는 `build_bilingual_ass` 이벤트별 태그. style.y 가 있으면 위/아래 자동 배치 대신
  `\an2\pos(cx, y×height)` — 사람이 정한 위치가 이긴다. B(subtitle) 는 `build_ass` 동일.
- B replace(Pillow 재합성)는 후순위 — 이번 판 미지원(오버라이드는 ass/srt 에만 반영).

## 검수 노출(읽기) 스키마 — ves 어댑터/review_meta 용

### SHOTCONE `localize_ja/metadata.json` → `ko_ja_pairs` (8/20 확장)

```json
{ "top_title": {"ko": "…", "ja": "…"},
  "subs":   [{"idx": 0, "start": 1.2, "end": 4.0, "ko": "…", "ja": "…", "style": {…}?}],
  "tts":    [{"idx": 0, "start": 5.0, "end": 8.5, "ko": "…", "ja": "…"}],
  "telops": [{"idx": 2, "start": 3.1, "end": 6.2, "ko": "…", "ja": "…", "style": {…}?}] }
```

- subs.end = **클램프·오버라이드 반영 후의 실표시 값**(l3_apply 와 같은 규칙).
- tts.start/end = cue 계획 창(편집 불가·표시용).
- ⚠ **telops 좌표 전환(이 판부터)**: 소스가 `onscreen_refined.json`(실제 렌더 목록)이고
  `idx = orig_index`(= translation.telops 의 index 좌표)다. 종전엔 `onscreen.json` 원시
  순번(kind 필터 없음)이라 translation·ass 매칭 좌표와 **어긋나 있었다(버그)** — 이
  판 전후로 같은 영상의 telops idx 가 달라질 수 있다. style 은 오버라이드가 있을 때 동봉.

### 잔망루피 C `outputs/{id}/ko_ja_pairs.json` (8/20 확장)

```json
{ "subs": [{"idx": 0, "start": 1.2, "end": 3.4, "end_actual": true,
            "ko": "…", "ja": "…", "style": {…}?, "end_fixed": true?}] }
```

- 1차 기록(합성 전)은 `end_actual: false`(계획값). retime 후 실표시 end 로 갱신되며
  `end_actual: true`. 빈 대사로 필터된 세그는 계획값으로 남는다.
- `end_fixed: true` = 사용자 지정 end(retime 미적용 — 위 규칙).

### 잔망루피 BJ/B `outputs/{id}/ja_events.json` (신설 — render 가 항상 떨군다)

```json
{ "video_id": "…", "coord": "translations.json entries 순번(entry_idx)",
  "events": [{"entry_idx": 3, "start": 2.0, "end": 5.5, "text": "…",
              "position": "bottom-center", "bbox": [x1, y1, x2, y2],
              "style": {…}|null, "end_fixed": false}] }
```

- BJ/B 타이밍은 detections 기반(샘플 간격 ≈0.5s 양자화)이라 translations.json 만으론
  표시 구간을 알 수 없다 — review_meta 는 이 파일로 타이밍·현재 스타일을 노출한다.
- `entry_idx` 가 오버라이드 좌표(`subs{idx}`). 미매칭(번역 없음 등)은 null.

## 하위호환·배포 게이트

- 구 엔진(이 변경 전)의 병합 함수는 dict 값에서 `"ja"` 외 키를 **조용히 무시**한다
  (process_video.py·dub.py 종전 코드). 즉 style/타이밍을 실은 오버라이드가 구 노드에
  가면 **에러 없이 미반영**으로 나간다(fail-loud 아님). 따라서 **오케스트레이터는 이
  변경이 전 노드에 배포된 것을 확인한 뒤 플래그로 편집 UI 를 연다** — 배포 기준 sha 는
  이 계약이 포함된 커밋(완료 보고에 명시).
- SHOTCONE 대사 style 렌더는 ai-video **69e5c06 이상**(v3 F-407/F-410) 노드 전제.

## 후속(이번 판 범위 밖)

- tts 의 style(디자인 레벨 KR 공유 해제)·start_sec/end_sec(재합성 창 재계산) 편집.
- B replace(Pillow) 경로의 style 반영.
