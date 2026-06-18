"""engine/common.py — 경로/JSON/.env 헬퍼."""
import os
import pathlib
import tempfile

from engine import common


def test_resolve_path_absolute():
    assert str(common.resolve_path("/tmp/x")) == "/tmp/x"


def test_resolve_path_relative():
    assert common.resolve_path("config/x.yaml") == common.PROJECT_ROOT / "config" / "x.yaml"


def test_ensure_dir_creates():
    with tempfile.TemporaryDirectory() as d:
        p = common.ensure_dir(pathlib.Path(d) / "a" / "b")
        assert p.is_dir()


def test_write_read_json_roundtrip_unicode():
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "sub" / "x.json"
        common.write_json({"k": "값", "n": 3}, p)
        assert common.read_json(p) == {"k": "값", "n": 3}


def test_load_env_manual_parse():
    with tempfile.TemporaryDirectory() as d:
        envp = pathlib.Path(d) / ".env"
        envp.write_text('LOOPY_TEST_KEY="bar123"\n# comment\nEMPTY\n', encoding="utf-8")
        os.environ.pop("LOOPY_TEST_KEY", None)
        common.load_env(envp)
        assert os.environ.get("LOOPY_TEST_KEY") == "bar123"
        os.environ.pop("LOOPY_TEST_KEY", None)


def test_get_secret_fallback_and_optional():
    os.environ.pop("PRIMARY_K", None)
    os.environ["FALLBACK_K"] = "v9"
    try:
        assert common.get_secret("PRIMARY_K", "FALLBACK_K") == "v9"
        assert common.get_secret("DOES_NOT_EXIST_K") is None
    finally:
        os.environ.pop("FALLBACK_K", None)


def _capture_run():
    """common._run 를 스텁해 ffmpeg 명령을 캡처(실제 실행/ffmpeg/미디어 불필요)."""
    calls = []
    orig = common._run
    common._run = lambda cmd, quiet=True: calls.append(cmd)
    return calls, orig


def test_frames_to_video_mp4_has_faststart():
    calls, orig = _capture_run()
    try:
        common.frames_to_video("/tmp/x", "/tmp/out.mp4", 30, codec="libx264")
    finally:
        common._run = orig
    assert "-movflags" in calls[0] and "+faststart" in calls[0]


def test_frames_to_video_ffv1_no_faststart():
    calls, orig = _capture_run()
    try:
        common.frames_to_video("/tmp/x", "/tmp/out.mkv", 30, codec="ffv1")
    finally:
        common._run = orig
    assert "+faststart" not in calls[0]  # mkv 무손실 중간본엔 불필요


def test_mux_audio_faststart_both_branches():
    calls, orig = _capture_run()
    try:
        common.mux_audio("/tmp/v.mp4", None, "/tmp/o1.mp4")          # 오디오 없음(복사)
        common.mux_audio("/tmp/v.mp4", "/tmp/a.wav", "/tmp/o2.mp4")  # 오디오 merge
    finally:
        common._run = orig
    assert all("+faststart" in c for c in calls)


def test_mux_dub_faststart_and_amix():
    calls, orig = _capture_run()
    try:
        common.mux_dub("/tmp/v.mp4", "/tmp/dub.wav", "/tmp/out.mp4", bg_volume=0.3)
    finally:
        common._run = orig
    cmd = calls[0]
    assert "+faststart" in cmd
    assert any("amix" in part for part in cmd)       # 원본+더빙 믹스
    assert any("loudnorm" in part for part in cmd)   # 라우드니스 정규화
    assert cmd.count("-i") == 2                       # bg_audio 없음 → 입력 2개(영상+더빙)


def test_mux_dub_with_bg_audio_three_inputs():
    calls, orig = _capture_run()
    try:
        common.mux_dub("/tmp/v.mp4", "/tmp/dub.wav", "/tmp/out.mp4", bg_audio="/tmp/novocals.wav")
    finally:
        common._run = orig
    cmd = calls[0]
    assert cmd.count("-i") == 3                   # 영상 + 반주스템 + 더빙
    assert "/tmp/novocals.wav" in cmd
