"""src/dub.py — Level C 가드 / 타임코드 / 자막 파싱 순수 로직 + 더빙 견고화 로직."""
import pathlib
import tempfile

from src.dub import (_ass_time, _detect_lang, _fit_speed, _needs_truncate, _norm_scale,
                     _srt_time, atempo_filters, build_alignment_report, dub_backend,
                     parse_segments, require_level_c, segment_ext, segment_hard_caps,
                     synthesize_segment, synthesize_with_retry)


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


# ── 더빙 견고화 로직 (싱크/환각방지/클리핑) ───────────────────────────────
def test_fit_speed_clamps_speedup():
    # 너무 길면(10s→2s) max_speedup 로 클램프(과속 방지)
    assert _fit_speed(10.0, 2.0, max_speedup=1.5) == 1.5


def test_fit_speed_clamps_slowdown():
    # 너무 짧으면(1s→5s) 최소 감속비로 클램프(과도한 늘림 방지)
    assert _fit_speed(1.0, 5.0, max_speedup=1.5, min_slowdown=0.7) == 0.7


def test_fit_speed_ratio_within_bounds():
    assert abs(_fit_speed(3.0, 2.0, max_speedup=1.6) - 1.5) < 1e-9
    assert _fit_speed(0.0, 2.0) == 1.0          # 잘못된 입력 보호


def test_segment_hard_caps_prevents_overrun():
    # 각 세그는 '다음 세그 시작'을 침범하면 안 됨(드론/겹침 방지)
    caps = segment_hard_caps([(0, 2), (2, 4), (10, 12)], guard=0.05, tail=0.5)
    assert abs(caps[0] - (2 - 0 - 0.05)) < 1e-9   # 다음 시작 2 직전까지
    assert abs(caps[1] - (10 - 2 - 0.05)) < 1e-9  # 다음 시작 10 직전까지(긴 공백 허용)
    assert abs(caps[2] - (12 - 10 + 0.5)) < 1e-9  # 마지막: 슬롯 + tail


def test_synthesize_with_retry_accepts_first_short():
    calls = {"n": 0}
    def fake():
        calls["n"] += 1
        return (10, [0] * (5 * 10))               # 5초 → 허용
    sr, audio = synthesize_with_retry(fake, max_dur=8.0, tries=4)
    assert calls["n"] == 1 and len(audio) / sr == 5.0


def test_synthesize_with_retry_stops_at_first_acceptable():
    seq = iter([30, 4, 99]); calls = {"n": 0}
    def fake():
        calls["n"] += 1
        return (10, [0] * (next(seq) * 10))
    synthesize_with_retry(fake, max_dur=8.0, tries=5)
    assert calls["n"] == 2                         # 두 번째(4초)에서 멈춤


def test_synthesize_with_retry_returns_shortest_when_all_long():
    seq = iter([30, 20, 25, 40])                   # 전부 환각(>8s) → 최단 선택
    def fake():
        return (10, [0] * (next(seq) * 10))
    sr, audio = synthesize_with_retry(fake, max_dur=8.0, tries=4)
    assert len(audio) / sr == 20.0


def test_norm_scale():
    assert abs(_norm_scale(1.0, 0.9) - 0.9) < 1e-9
    assert _norm_scale(0.0, 0.9) == 1.0            # 무음(peak 0) 보호
    assert abs(_norm_scale(0.5, 0.9) - 1.8) < 1e-9


def test_needs_truncate():
    assert _needs_truncate(5.6, 4.95) is True
    assert _needs_truncate(4.9, 4.95) is False
    assert _needs_truncate(9.9, None) is False     # 캡 없음


def test_detect_lang_english_vs_default():
    assert _detect_lang("Okay!", "ja") == "en"
    assert _detect_lang("I'm Queen Loopy!", "ja") == "en"
    assert _detect_lang("おにぎりもクイーンみたいにぴ！", "ja") == "ja"
    assert _detect_lang("123!!!", "ja") == "ja"    # 문자 없음 → 기본값


def test_dub_backend_gptsovits():
    assert dub_backend({"dub": {"tts_backend": "gptsovits"}}) == "gptsovits"


def test_segment_ext_gptsovits_is_wav():
    assert segment_ext({"dub": {"tts_backend": "gptsovits"}}) == ".wav"
