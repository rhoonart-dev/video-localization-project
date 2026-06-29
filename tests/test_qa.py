"""engine/qa.py — 플래그 판정 / 요약 / 리포트 빌드 순수 로직."""
from engine.qa import build_report, flag_reason, seconds_to_tc, summarize

QCFG = {"psnr_warn_below": 30.0, "ssim_warn_below": 0.90, "motion_flag_threshold": 0.35}


def test_seconds_to_tc():
    assert seconds_to_tc(65.25) == "01:05.25"


def test_flag_reason_clean_is_empty():
    assert flag_reason(40.0, 0.99, 0.1, QCFG) == ""


def test_flag_reason_low_ssim():
    assert "SSIM" in flag_reason(40.0, 0.5, 0.1, QCFG)


def test_flag_reason_high_motion():
    assert "움직임" in flag_reason(40.0, 0.99, 0.9, QCFG)


def test_summarize_counts_flagged():
    m = [{"psnr": 30, "ssim": 0.9, "reason": ""},
         {"psnr": 20, "ssim": 0.8, "reason": "x"}]
    s = summarize(m)
    assert s["frames"] == 2 and s["flagged"] == 1


def test_build_report_contains_flag_table():
    m = [{"idx": 3, "ts": 1.0, "psnr": 20.0, "ssim": 0.5, "motion": 0.1,
          "reason": "SSIM낮음(0.500)"}]
    rep = build_report("vidX", m, {"qa": QCFG})
    assert "vidX" in rep and "검수 필요" in rep and "00:01.00" in rep
