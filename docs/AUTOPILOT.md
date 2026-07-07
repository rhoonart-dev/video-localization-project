# autopilot — 쇼츠 자동 현지화 파이프라인

원 채널(잔망루피)의 Shorts 를 **선별 → 현지화 → 예약 업로드**까지 자동화하는 레이어.
기존 현지화 엔진(`src/process_video`, `src/dub` 등)을 스텝으로 감싼다.

> **원칙 유지**: README 의 "비가역 동작 자동화 금지" 가드는 재해석해 유지한다 —
> 준비(선별·현지화·QA·예약 초안)는 전부 자동, **공개 전환 승인은 항상 사람**.

## 단계 로드맵

| Phase | 범위 | 상태 |
|---|---|---|
| **1. 선별 봇** | scan(채널 수집) → score(일본 적합도) → report(후보 TOP N). 업로드 없음 | ✅ 구현됨 |
| **2. 반자동** | selected 건 자동 현지화(+레벨 실측 판별) → 자동 QA → 승인 게이트 → private+예약 업로드 | 설계 |
| **3. 완전 자동** | KPI 피드백, QA 통과 건 무승인 예약 공개(실패만 알림) | 운영 후 판단 |

## Phase 1 사용법

```bash
# 0) (권장) .env 에 YOUTUBE_API_KEY — 없으면 yt-dlp 폴백(근사 조회수, 댓글 신호 생략)
python -m src.autopilot scan                # 채널 Shorts → 원장(outputs/autopilot.db)
python -m src.autopilot score --limit 30    # 발견분 스코어링(공개지표 + Gemini)
python -m src.autopilot report --top 10     # outputs/autopilot_report.md (+csv)
python -m src.autopilot status              # 상태별 집계
python -m src.autopilot mark <id> --state selected   # 처리 시작 결정(사람)
```

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

### ⚠ 레벨(추정)의 한계

리포트의 레벨은 **제목 기반 LLM 추정**이다. 처리 전 반드시 프레임 검사로 번인 자막
유무를 실측할 것 — 번인 없는 Short 에 Level B 를 돌리면 OCR 이 노이즈를 오검출한다
(2026-07-01 `loopy_short` 사례). Phase 2 에서 프레임 샘플 OCR 자동 판별로 대체 예정.

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

## 운영(Phase 2+에서)

- 스케줄: launchd LaunchAgent(StartCalendarInterval) + `caffeinate -i` 래핑
  (cron 과 달리 슬립 중 놓친 실행을 깨어날 때 보충 실행)
- 알림: Telegram 봇(실패·승인 요청) — 공개 서버 불필요(long polling)
- 승인 게이트: `autopilot approve <id>` CLI 또는 Telegram 버튼 → 원장 상태 전이
- 클라우드 CI 는 실처리 부적합(4GB+ 가중치, 느린 CPU) — 코드 테스트 전용
