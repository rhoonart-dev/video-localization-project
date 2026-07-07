"""src/autopilot.py Phase 2 — QA 게이트·업로드 패키지 순수 로직 (처리 실행 없음)."""
import tempfile
from pathlib import Path

from src.autopilot import build_upload_text, final_video_for, qa_verdict

GATE = {"max_flag_ratio": 0.5, "min_ssim": 0.85}


def test_qa_verdict_pass_hold():
    ok, why = qa_verdict({"frames": 100, "flagged": 10, "ssim_avg": 0.95, "psnr_avg": 30}, GATE)
    assert ok == "pass" and "10%" in why
    hold, why2 = qa_verdict({"frames": 100, "flagged": 80, "ssim_avg": 0.95, "psnr_avg": 30}, GATE)
    assert hold == "hold" and "80%" in why2
    hold2, _ = qa_verdict({"frames": 100, "flagged": 0, "ssim_avg": 0.5, "psnr_avg": 30}, GATE)
    assert hold2 == "hold"


def test_qa_verdict_no_measurement_passes():
    # Level A/C 는 인페인트 비교가 없음 → 측정 0 = 게이트 통과(사람 검수는 그대로)
    ok, why = qa_verdict({"frames": 0, "flagged": 0, "ssim_avg": 0.0, "psnr_avg": 0.0}, GATE)
    assert ok == "pass" and "측정 없음" in why


def test_final_video_for_route():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        assert final_video_for("B", base) is None            # 아직 산출물 없음
        (base / "final_draft.mp4").write_bytes(b"x")
        assert final_video_for("B", base).name == "final_draft.mp4"
        (base / "final_dubbed.mp4").write_bytes(b"x")
        assert final_video_for("C", base).name == "final_dubbed.mp4"
        (base / "final_dubbed_subbed.mp4").write_bytes(b"x")  # 자막 번인본이 우선
        assert final_video_for("C", base).name == "final_dubbed_subbed.mp4"
        assert final_video_for("A", base) is None             # A = 무변환(원본 사용)


def test_build_upload_text_contents():
    meta = {"title_candidates": ["ルーピー登場!", "かわいいルーピー"],
            "description": "説明文です\n\n© IP", "hashtags": ["#ルーピー", "#Shorts"],
            "tags": ["loopy", "japan"]}
    row = {"video_id": "abc", "title": "루피 원제", "url": "https://youtube.com/shorts/abc"}
    txt = build_upload_text(meta, row, route="C", qa_note="pass — 측정 없음")
    assert "ルーピー登場!" in txt                 # 제목 후보
    assert "説明文です" in txt
    assert "#ルーピー" in txt
    assert "루피 원제" in txt                     # 원본 추적 정보
    assert "defaultAudioLanguage=ja" in txt       # 업로드 시 설정 항목
    assert "madeForKids" in txt                   # 정책 미결정 리마인더
    assert "19:00 JST" in txt                     # 예약 권장 시간
