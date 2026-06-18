"""src/dub.py — Level C 가드 / 타임코드 / 자막 파싱 순수 로직."""
import pathlib
import tempfile

from src.dub import (_ass_time, _srt_time, atempo_filters, build_alignment_report,
                     dub_backend, parse_segments, require_level_c, segment_ext,
                     synthesize_segment)


def test_atempo_filters_single_when_in_range():
    assert atempo_filters(1.0) == "atempo=1.0000"
    assert atempo_filters(1.5) == "atempo=1.5000"


def test_atempo_filters_chains_above_2x():
    # 3.0배는 2.0 × 1.5 로 분해
    assert atempo_filters(3.0) == "atempo=2.0,atempo=1.5000"


def test_dub_backend_default_and_override():
    assert dub_backend({}) == "elevenlabs"
    assert dub_backend({"dub": {"tts_backend": "xtts"}}) == "xtts"


def test_segment_ext_by_backend():
    assert segment_ext({"dub": {"tts_backend": "xtts"}}) == ".wav"
    assert segment_ext({}) == ".mp3"


def test_synthesize_xtts_requires_speaker_wav():
    raised = False
    try:
        synthesize_segment("こんにちは", {"dub": {"tts_backend": "xtts"}}, speaker_wav=None)
    except ValueError:
        raised = True
    assert raised


def test_synthesize_elevenlabs_requires_voice_id():
    raised = False
    try:
        synthesize_segment("x", {"dub": {"tts_backend": "elevenlabs"}}, voice_id=None)
    except ValueError:
        raised = True
    assert raised


def test_require_level_c_allows_c():
    require_level_c("C")  # 예외 없음


def test_require_level_c_rejects_others():
    raised = False
    try:
        require_level_c("A")
    except ValueError:
        raised = True
    assert raised


def test_srt_and_ass_time_parsing():
    assert abs(_srt_time("00:00:01,500") - 1.5) < 1e-9
    assert abs(_ass_time("0:00:01.50") - 1.5) < 1e-9


def test_parse_segments_srt():
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "ja.srt"
        p.write_text("1\n00:00:00,000 --> 00:00:01,000\nやあ\n\n"
                     "2\n00:00:01,000 --> 00:00:02,000\n元気\n", encoding="utf-8")
        segs = parse_segments(str(p))
        assert len(segs) == 2
        assert segs[0]["text"] == "やあ"
        assert abs(segs[1]["end"] - 2.0) < 1e-9


def test_parse_segments_ass_strips_tags():
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "ja.ass"
        p.write_text("[Events]\n"
                     "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,{\\an2}やあ\\Nまた\n",
                     encoding="utf-8")
        segs = parse_segments(str(p))
        assert len(segs) == 1
        assert "やあ" in segs[0]["text"] and "また" in segs[0]["text"]
        assert "an2" not in segs[0]["text"]


def test_build_alignment_report():
    segs = [{"start": 0.0, "end": 1.0, "text": "やあ"}]
    r = build_alignment_report("vid", segs, "voiceX")
    assert r["segment_count"] == 1
    assert r["voice_id"] == "voiceX"
    assert r["total_speech_sec"] == 1.0
