# autopilot — 쇼츠 자동 현지화 파이프라인

원 채널(잔망루피)의 Shorts 를 **선별 → 현지화 → 예약 업로드**까지 자동화하는 레이어.
기존 현지화 엔진(`src/process_video`, `src/dub` 등)을 스텝으로 감싼다.

> **원칙 유지**: README 의 "비가역 동작 자동화 금지" 가드는 재해석해 유지한다 —
> 준비(선별·현지화·QA·예약 초안)는 전부 자동, **공개 전환 승인은 항상 사람**.

## 단계 로드맵

| Phase | 범위 | 상태 |
|---|---|---|
| **1. 선별 봇** | scan(채널 수집) → score(일본 적합도) → report(후보 TOP N). 업로드 없음 | ✅ 구현됨 |
| **2. 반자동** | process(다운로드→실측판별→현지화→QA) → pending/approve(업로드 패키지) → uploaded 기록. API 감사 전이라 **업로드 클릭은 사람**(YouTube Studio) | ✅ 구현됨 |
| **3. 완전 자동** | KPI 피드백, QA 통과 건 무승인 예약 공개(실패만 알림) | 운영 후 판단 |

## Phase 1 사용법

```bash
# 0) (권장) .env 에 YOUTUBE_API_KEY — 없으면 yt-dlp 폴백(근사 조회수, 댓글 신호 생략)
python -m src.autopilot scan                # 채널 Shorts → 원장(outputs/autopilot.db)
python -m src.autopilot score --limit 30    # 발견분 스코어링(공개지표 + Gemini)
python -m src.autopilot report --top 10     # outputs/autopilot_report.md (+csv)
python -m src.autopilot status              # 상태별 집계
python -m src.autopilot mark <id> --state selected   # 처리 시작 결정(사람)
python -m src.autopilot rescore [id]        # scored → discovered 재채점(전량 스캔 후 권장)

# Phase 2 — 처리~승인 (업로드 클릭은 사람)
python -m src.autopilot process [--limit 3] [--video-id <id>]
#   다운로드(yt-dlp) → 실측 판별(프레임 OCR+ASR: B=번인/C=더빙/A=무변환)
#   → 현지화(B=process_video, C=src.dub·GPT-SoVITS) → 메타데이터(실측 대사 컨텍스트) → QA 게이트
python -m src.autopilot pending             # 승인 대기(검수 경로 포함)
python -m src.autopilot approve <id>        # → outputs/<id>/upload_package/ (영상+UPLOAD.md 체크리스트)
python -m src.autopilot uploaded <id> --url <URL>   # 사람이 업로드 후 기록(종착)
```

운영 순서 권장: **전량 `scan` 완료 후 `score`** — 점수의 조회수 성분이 원장 내 상대값이라,
스캔 도중 채점하면 정규화 기준이 흔들린다(흔들렸으면 `rescore` 후 재채점).
LLM 전면 장애·YouTube 쿼터 소진 시 실행이 스스로 중단되고 남은 건 discovered 로 남는다
(`require_llm: false` 로 정량만 채점 가능하나 비권장).

설정: `config/pipeline.config.yaml` 의 `autopilot:` 섹션(채널·가중치·비용 가드).

### 스코어 신호 (가중치는 config)

- `views` (0.20) — 조회수 log 정규화 (원장 내 상대 평가)
- `like_ratio` (0.15) — 좋아요율, 5% 상한
- `jp_comments` (0.35) — **댓글 일본어(가나) 비율** — 일본 반응의 가장 좋은 공개 신호
- `llm_jp_fit` (0.30) — LLM 판정: 일본 밈 적합도 + 언어 의존도 패널티

없는 신호(댓글 비활성·LLM 실패)는 가중치 재정규화로 흡수 — 파이프라인이 멈추지 않는다.

### 상태 머신 (`src/ledger.py`, SQLite WAL)

```
discovered → scored → selected → processing → qa_passed → pending_approval → approved → uploaded
                    ↘ skipped                (어디서든 failed)
```

Phase 1 은 `scored` 까지 자동. `selected/skipped` 는 `mark` 로 사람이 기록.

### ⚠ 라우트(autopilot) ≠ Level(README/process_video) — 같은 글자, 다른 축

- **리포트의 레벨(추정)**: 제목 기반 LLM 추정 — 참고용. `process` 가 실측으로 덮어쓴다.
- **precheck 실측 라우트**: `B`=화면 한국어 자막 실측 → `process_video --level B`(캡션 교체) /
  `C`=번인 없음+대사 실측 → `src.dub`(더빙 — **인페인트를 타지 않는다**, README Level C 와 다름) /
  `A`=둘 다 없음 → **무변환**(메타데이터만, README Level A 의 '자막 사이드카'와 다름).
- 정책: **번인+대사 둘 다 있으면 B**(캡션 교체 우선, 더빙 안 함 — 필요 시 사람이 후처리 결정).
- pending 목록의 `[C]` 를 보고 `src.process_video --level C` 를 수동 실행하지 말 것 —
  번인 없는 영상에 인페인트를 돌리는 오검출 사고(2026-07-01 loopy_short)가 재현된다.

## Phase 2 전제 조건 (지금 시작해야 하는 것)

1. **YouTube API 감사(audit) 신청** — 리드타임 최장. 미검증 프로젝트로 업로드하면
   영상이 private 으로 잠기고 이의신청 불가(2020-07-28 이후 정책, 2026 현재 유효).
   - GCP 프로젝트 생성 → YouTube Data API v3 활성화 → OAuth 클라이언트(데스크톱)
   - "YouTube API Services — Audit and Quota Extension Form" 제출
   - 참고: 업로드 쿼터는 2026-06 부터 전용 버킷 100편/일(충분)
2. **일본어 폰트 배치** — `fonts/NotoSansJP-*.ttf` (OFL, 상업 가능). 현재 비어 있음.
3. **루피 레퍼런스 음성 은행** — 더빙 음색 품질의 전제.
   `~/Downloads/loopy_jp_localized/루피음성_데이터셋/` 보컬 분리본 4개가 출발점.
4. **madeForKids 정책 결정** — 잔망루피는 성인 밈 캐릭터지만 외견은 키즈.
   오분류 시 COPPA/FTC 리스크 → 채널 단위로 한 번 확정 필요.
5. **업로드 페이스 정책** — YPP "inauthentic content"(2025-07 개정) 리스크 회피:
   하루 1~3편, 더빙·현지화 부가가치 명확화, 동일 템플릿 대량 살포 패턴 금지.

## 운영 (구현됨 — launchd + Slack)

- **일일 자동 실행**: `launchd` 가 매일 07:00 `scripts/autopilot_daily.sh` 실행
  (`~/Library/LaunchAgents/com.rhoonart.loopy-autopilot.plist`). 슬립 중이었으면
  깨어날 때 보충 실행. 로그: `outputs/autopilot_daily.log`.
  - daily = scan → score → process(selected 있으면) → report → **Slack 다이제스트**
  - 끄기: `launchctl bootout gui/$(id -u)/com.rhoonart.loopy-autopilot`
  - 수동 1회: `launchctl kickstart -k gui/$(id -u)/com.rhoonart.loopy-autopilot`
- **Slack 알림**(`src/notify.py`, `.env` 의 SLACK_WEBHOOK_URL — 커밋 금지):
  일일 다이제스트(후보 TOP·승인 대기·상태), 처리 완료/실패, 승인·패키지 완료.
  미설정이면 조용히 생략 — 알림 장애가 파이프라인을 죽이지 않는다.
- **사람의 리듬**: Slack 다이제스트 보고 `mark <id> --state selected` → 다음 daily 가
  처리 → "처리 완료" 알림 오면 검수 후 `approve <id>` → Studio 업로드 → `uploaded <id> --url`.
- **더빙 품질**(자동): self-ref(영상 자체 목소리 클로닝, `dub.gptsovits.self_ref`) +
  피치 매칭 후보 선택(`pitch_match_tries`) + ASR 할루시네이션 필터(`asr_max_no_speech`).
- 클라우드 CI 는 실처리 부적합(4GB+ 가중치, 느린 CPU) — 코드 테스트 전용
