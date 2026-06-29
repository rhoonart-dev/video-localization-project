"""loopy-jp 자체 현지화 엔진.

레이어:
  detect  → OCR 텍스트 탐지·스타일 추출
  mask    → 마스크 생성 + temporal smoothing
  inpaint → 텍스트 제거(배경 복원) 백엔드 팩토리
  translate → LLM 트랜스크리에이션
  render  → 일본어 텍스트 원본 스타일 재합성
  qa      → PSNR/SSIM + 아티팩트 플래그

데이터 계약은 engine/schemas.py 의 dataclass 와 JSON 직렬화로 통일한다.
"""

__version__ = "0.1.0"
