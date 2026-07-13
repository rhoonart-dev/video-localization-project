"""src/precheck.py — 레벨 실측 판별 순수 로직 (OCR/ASR 실행 없음)."""
from src.precheck import (decide_route, ensure_korean_capable, hangul_chars,
                          solid_hit_frames)


def test_ensure_korean_capable_blocks_silent_rapidocr_fallback():
    # paddle 초기화 실패 → rapidocr 폴백은 한국어 불가 → 번인 판정이 항상 0 이 되어
    # 라우트가 뒤집힌다(B→C/A). 조용히 진행하지 말고 실패해야 한다.
    ensure_korean_capable("paddleocr", "paddleocr")            # 정상 — 통과
    try:
        ensure_korean_capable("rapidocr", "paddleocr")
        assert False, "한국어 불가 폴백을 거부해야 함"
    except RuntimeError:
        pass
    ensure_korean_capable("easyocr", "paddleocr")              # 한국어 가능 폴백은 허용


def test_hangul_chars_counts_only_hangul():
    assert hangul_chars("루피 귀여워") == 5
    assert hangul_chars("ルーピー kawaii 123") == 0
    assert hangul_chars("") == 0


def test_solid_hit_frames_filters_ocr_noise():
    # loopy_short 함정: conf 0.2~0.4 단문자("2",".","X")는 노이즈 — 걸러져야 함.
    frames = [
        {"frame_idx": 0, "regions": [{"text": "2", "confidence": 0.19}]},
        {"frame_idx": 15, "regions": [{"text": ".", "confidence": 0.36},
                                      {"text": "605", "confidence": 0.29}]},
        {"frame_idx": 30, "regions": []},
    ]
    assert solid_hit_frames(frames, min_conf=0.75, min_hangul=2) == 0
    # 진짜 번인 자막: 고신뢰 + 한글 2자 이상 → 프레임 수 카운트
    frames2 = [
        {"frame_idx": 0, "regions": [{"text": "마라엽떡 먹방", "confidence": 0.97}]},
        {"frame_idx": 15, "regions": [{"text": "마라엽떡 먹방", "confidence": 0.95}]},
        {"frame_idx": 30, "regions": [{"text": "!!", "confidence": 0.9}]},   # 한글 없음 → 제외
    ]
    assert solid_hit_frames(frames2, min_conf=0.75, min_hangul=2) == 2


def test_decide_route_burn_plus_dialogue_is_bc():
    # 번인+대사(먹방류) → BC: 캡션 제거(인페인트) + 더빙 (2026-07-09 이중자막 사고로 신설)
    assert decide_route(burn_frames=3, dialogue_segs=2, min_persist=2) == "BC"


def test_decide_route_burn_only_is_b():
    assert decide_route(burn_frames=3, dialogue_segs=0, min_persist=2) == "B"


def test_decide_route_dialogue_no_burn_is_dub():
    # 번인 없음 + 대사 있음 → C (loopy_short "루피" 사례 — dub_from_video 플로우)
    assert decide_route(burn_frames=0, dialogue_segs=1, min_persist=2) == "C"
    assert decide_route(burn_frames=1, dialogue_segs=1, min_persist=2) == "C"  # 지속성 미달=노이즈


def test_decide_route_nothing_to_localize():
    # 번인도 대사도 없음 → A (영상 무변환, 메타데이터만)
    assert decide_route(burn_frames=0, dialogue_segs=0, min_persist=2) == "A"


def test_caption_margin_v_places_subtitle_above_bottom_captions():
    from src.precheck import caption_margin_v
    H = 1080
    # 데모 실측: 하단 캡션 y 972~1038 → 자막은 그 위 (H-972+pad)
    frames = [{"regions": [
        {"text": "마라 엽떡, 엽기 닭발", "confidence": 0.9, "bbox": [400, 972, 1500, 1038]}]}]
    m = caption_margin_v(frames, H, min_conf=0.75, min_hangul=2, pad=16)
    assert m == 1080 - 972 + 16                       # = 124
    # 중간 화면 카드(하단 아님)는 무시 → 기본 마진
    mid = [{"regions": [
        {"text": "퀸즈정식 소개 카드", "confidence": 0.95, "bbox": [300, 500, 900, 754]}]}]
    assert caption_margin_v(mid, H, 0.75, 2) == 30
    # 캡션 없음 → 기본
    assert caption_margin_v([], H, 0.75, 2) == 30
    # 하단 밴드가 과도하게 높아도 화면 45% 넘게 못 올라감(클램프)
    tall = [{"regions": [
        {"text": "큰 자막 큰 자막", "confidence": 0.9, "bbox": [0, 400, 1900, 1060]}]}]
    assert caption_margin_v(tall, H, 0.75, 2) <= int(H * 0.45)
    # 저신뢰/비한글은 무시
    noise = [{"regions": [{"text": "!!", "confidence": 0.9, "bbox": [0, 1000, 100, 1050]},
                          {"text": "마라", "confidence": 0.3, "bbox": [0, 1000, 100, 1050]}]}]
    assert caption_margin_v(noise, H, 0.75, 2) == 30
