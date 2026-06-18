# fonts/ — 일본어 폰트 (git 무시, 라이선스 주의)

`config/font_map.yaml` 이 참조하는 일본어 폰트 파일을 여기 둔다.

## 권장 (Open Font License — 상업 가능, 임베딩 가능)
- Noto Sans JP (Black / Bold / Medium / Regular)
- Noto Serif JP (Bold)

설치 예:
```bash
# Google Fonts 에서 Noto Sans JP / Noto Serif JP 내려받아 .ttf 를 이 폴더에 배치
# 예) NotoSansJP-Black.ttf, NotoSansJP-Bold.ttf, NotoSansJP-Medium.ttf,
#     NotoSansJP-Regular.ttf, NotoSerifJP-Bold.ttf
```

## 주의
- 잔망루피 한국어 밈 폰트와 **분위기 맞는** 일본어 폰트를 미리 선정해 `font_map.yaml` 에 매핑.
- 상업 채널: 폰트 **임베딩/방송 사용 라이선스**를 반드시 확인(특히 유료/제한 폰트).
- 폰트 파일은 라이선스에 따라 저장소 커밋이 금지될 수 있어 기본 `.gitignore` 처리됨.
