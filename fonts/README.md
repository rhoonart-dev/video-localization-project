# fonts/ — 일본어 폰트 (git 무시, 라이선스 주의)

`config/font_map.yaml` 이 참조하는 일본어 폰트 파일을 여기 둔다.

## 권장 (Open Font License — 상업 가능, 임베딩 가능)
- Noto Sans JP (Black / Bold / Medium / Regular)
- Noto Serif JP (Bold)

설치 예 (2026-07-23 실배치 — noto-cjk 공식 JP 서브셋 OTF, font_map.yaml 과 일치):
```bash
cd fonts
for w in Black Bold Medium Regular; do
  curl -sL -o "NotoSansJP-$w.otf" \
    "https://github.com/notofonts/noto-cjk/raw/main/Sans/SubsetOTF/JP/NotoSansJP-$w.otf"
done
curl -sL -o NotoSerifJP-Bold.otf \
  "https://github.com/notofonts/noto-cjk/raw/main/Serif/SubsetOTF/JP/NotoSerifJP-Bold.otf"
```

## 주의
- 잔망루피 한국어 밈 폰트와 **분위기 맞는** 일본어 폰트를 미리 선정해 `font_map.yaml` 에 매핑.
- 상업 채널: 폰트 **임베딩/방송 사용 라이선스**를 반드시 확인(특히 유료/제한 폰트).
- 폰트 파일은 라이선스에 따라 저장소 커밋이 금지될 수 있어 기본 `.gitignore` 처리됨.
