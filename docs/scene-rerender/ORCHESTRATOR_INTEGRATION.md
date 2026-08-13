# ves-orchestrator 통합 진단·방안 — 2026-08-13

증상: 이 프로젝트(macmini-luna3 로컬)의 현지화 결과가 가장 좋았는데, ves-orchestrator 로
돌리면 결과가 이상함. 원인 분석과 "이 세션 품질을 오케스트레이터에서 재현"하는 방안.

## 1. 원인 — 오케스트레이터는 **다른 엔진**을 부르고 있다

`ves/adapters/localize.py`(2026-08-10 배선)는 이 프로젝트의 `scripts/localize_run.py` 를
부르지 않는다. 호출부는:

```
<engine>/.venv/bin/python -m src.process_video --video <mp4> --video-id <run_id> --level B
```

즉 "video-localization-project" 라는 이름을 가진 것이 현재 **셋**이고 서로 다르다:

| # | 위치 | 실체 | 상태 |
|---|---|---|---|
| ① | macmini-luna3 `~/ves/video-localization-project` | **scene-rerender** — job 디렉토리 체크포인트 교체 + ai-video 재렌더 (좋았던 결과의 출처) | git 미등록이었음 → **이 커밋으로 ③ 에 편입** |
| ② | GitHub `rhoonart-da/video-localization-project` | 잔망루피 KR숏폼→JP (`app.cli`, 자막 번인·fulldub·ElevenLabs) | 별개 리포 — 플릿에 배포되지 않음 |
| ③ | GitHub `rhoonart-dev/video-localization-project` = **이 리포(정본)** | loopy-jp — 완성 mp4 후처리(OCR→인페인팅→트랜스크리에이션→재합성), `src.process_video` | `deployments` 가 추적·배포. 어댑터의 level A/B 경로가 부르는 것 |

## 2. 왜 결과가 나쁜가 — 입력 계약이 다르다

어댑터는 스토리지에서 **완성된 shorts.mp4 한 파일만** 내려받아 후처리한다.
이 세션 품질의 원천이 전부 소실된다:

| 이 세션(①) | 오케스트레이터 경로(③) |
|---|---|
| 자막·제목·TTS 를 **데이터 계층에서 교체** 후 클린 재렌더 (한국어 텍스트가 화면에 아예 안 박힘) | 한국어 제목·밴드·자막이 **이미 번인된 영상 위** 후처리 |
| 텔롭 원문 대조로 ASR 오청취 교정(멀티 그루브 등) | 전사 데이터 접근 불가 |
| gen_flags 반복으로 컷 프레임 단위 재현 → 자막 싱크 보장 | 재렌더 자체가 불가(체크포인트 없음) |
| 용어집(멤버명)·11자 제목 규칙·한일 병기 고지·JP TTS 재합성 | 없음 (잔망루피/범용 로직) |

## 3. 방안 — "엔진 ①을 정본으로 오케스트레이터에 편입"

1. **레포 통합**: ① 을 **정본 ③ 리포**에 `scene-rerender` 모드로 편입(이 커밋).
   플릿이 배포받는 것이 ③ 이고, ①이 git 밖에 있는 한 updater 가 배포할 수 없기 때문이다.
   기존 level A/B 경로와 **신규 파일로만 공존**하고(scripts/·config/locales.json·docs/·tests/),
   어댑터가 `params.mode` 로 갈라 부른다.
2. **어댑터 계약 변경**: scene-rerender 잡은 파일 왕복이 아니라 **생성 노드 어피니티**로.
   - 근거: 이 파이프라인은 job 디렉토리(체크포인트)와 **원본 소스(편당 ~6GB)** 가 필요
     — 생성 노드에 이미 다 있다. GPU 불필요 → mm-06 캡도 불필요.
   - 오케스트레이터의 자기 지칭 캡(`node:mm-XX`, claim.py §6-1)을 localize 잡에 그대로 쓰면 됨.
   - 어댑터는 `localize_run.py --job-dir <생성 산출 디렉토리>` 호출 → 산출 `shorts.mp4` +
     `localize_ja/metadata.json` 을 `ves-localized` 에 업로드 → 기존 `localization_qa` 검수함
     흐름 유지(승인·공개는 사람).
3. **전제: 미커밋 변경 커밋** — updater 가 git 으로 엔진을 배포하므로, 커밋 안 된 개선은
   다른 노드에 존재하지 않는다(= "여기서만 잘 되는" 구조적 이유):
   - ai-video: `--design-subtitle-font` 플래그(cli.py) + **JP 폰트 파일**(현재 ArialUnicode
     untracked — 정식 폰트 선정 겸 assets 에 커밋하거나 부트스트랩 설치 스크립트에 명시)
   - brain: `work_publish_notice.json`(ヘミリイェチェパ 표기·한일 병기) — 오케스트레이터의
     brain publish 어댑터도 이 설정을 읽는다
   - localize_run 이 brain `loop_policy.json gen_flags_base` 를 읽으므로 워커 노드에 brain
     체크아웃 필요(부트스트랩에 이미 포함 — 확인만)
4. **컷오버 순서**: 엔진 편입·검증 전까지 `ops_config.jp_pipeline='off'` 유지(기본 off).
   ショトコン은 그때까지 기존 autopilot(brain scene_publish_loop 의 현지화 게이트)으로.
   편입 후 on 으로 올리고, 이중 생산 방지를 위해 autopilot 쪽 게이트를 끈다
   (locales.json 에서 채널 제거 또는 brain 루프의 ショトコン 배정 해제).

## 4. 하지 말 것

- ③의 level A/B(완성 mp4 후처리)로 혜미리예채파를 계속 돌리는 것 — 완성 영상 후처리는
  구조적으로 이 품질에 도달할 수 없다(§2). ③은 원본 데이터가 없는 외부 소스 영상
  (잔망루피처럼 남의 완성본을 받아오는 경우)에만 적합.
- ①을 별도 이름의 새 레포로 푸시하는 것 — 이름이 넷이 된다. **정본 ③ 에 모드로 편입이 정답**이다.
