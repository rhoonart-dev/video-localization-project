"""src/dub.py — Level C 가드 / 타임코드 / 자막 파싱 순수 로직 + 더빙 견고화 로직."""
import pathlib
import tempfile

from src.dub import (_ass_time, _detect_lang, _fit_speed, _is_dialogue, _needs_truncate,
                     _norm_scale, _srt_time, atempo_filters, build_alignment_report, dub_backend,
                     parse_segments, require_level_c, segment_ext, segment_hard_caps,
                     synthesize_segment, synthesize_with_retry)


def test_is_dialogue_keeps_real_sentences():
    assert _is_dialogue("그럼 맛있게 잘 먹겠습니다!")
    assert _is_dialogue("로제마라샹궈, 크림새우 그리고 치킨까지 준비해보았습니다")
    assert _is_dialogue("주먹밥도 퀸처럼!")


def test_is_dialogue_drops_reactions_and_noise():
    assert not _is_dialogue("앙! 앙! 앙! 앙!")      # 씹는소리 반복
    assert not _is_dialogue("음! 음! 음!")
    assert not _is_dialogue("아!")                   # 2음절 이하 감탄
    assert not _is_dialogue("냥!")
    assert not _is_dialogue("Excuse me!")            # 짧은 영어 추임새
    assert not _is_dialogue("1, 2, 3, 4, 5")         # 숫자 카운트
    assert not _is_dialogue("   ")


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


def test_reliable_segment_filters_hallucination():
    from src.dub import reliable_segment
    # 실측(아기루피 Short): no_speech 0.75 + '유료 광고 포함' 지어냄 → 제외돼야 함
    assert not reliable_segment(0.75, -0.92)
    assert reliable_segment(0.10, -0.30)            # 또렷한 실제 대사
    assert not reliable_segment(0.10, -1.5)         # 확신 없는 웅얼거림
    assert reliable_segment(0.5, -1.2)              # 경계값 포함


def test_loop_plan_gpt_sovits_3_to_10s_requirement():
    from src.dub import loop_plan
    assert loop_plan(5.0) == 1                     # 이미 3~10s
    assert loop_plan(2.35) == 2                    # 실측(loopy_short "루피"): x2 → 4.85s
    assert loop_plan(0.2) == 0                     # 유의미한 발화 아님
    assert loop_plan(0.0) == 0
    n = loop_plan(1.0)                             # 반복 결과가 3.2~10s 안
    assert n >= 3 and n * 1.0 + (n - 1) * 0.15 <= 10.0


def test_pick_ref_segments_greedy_cap():
    from src.dub import pick_ref_segments
    segs = [{"start": 0, "end": 4, "text": "가"}, {"start": 5, "end": 10, "text": "나"},
            {"start": 11, "end": 12, "text": "다"}]
    picked = pick_ref_segments(segs, max_total=8.0)
    assert [s["text"] for s in picked] == ["가"]    # 4+5>8 → 첫 세그까지만
    assert pick_ref_segments([], 8.0) == []
    assert [s["text"] for s in pick_ref_segments(segs, 20.0)] == ["가", "나", "다"]


def test_pitch_distance_octaves():
    from src.dub import pitch_distance_octaves
    assert pitch_distance_octaves(440, 440) == 0.0
    assert abs(pitch_distance_octaves(440, 220) - 1.0) < 1e-9   # 한 옥타브
    assert pitch_distance_octaves(0, 440) == float("inf")       # 측정 불가 = 최악
    # 실측 사례: 405Hz vs 274Hz ≈ 0.56 oct (기각), 405 vs 430 ≈ 0.09 oct (합격권)
    assert pitch_distance_octaves(405, 274) > 0.5
    assert pitch_distance_octaves(405, 430) < 0.15


def test_f0_median_synthetic_tone():
    import numpy as np
    from src.dub import f0_median
    sr = 16000
    t = np.arange(sr) / sr
    tone = np.sin(2 * np.pi * 300 * t)                          # 300Hz 정현파
    assert abs(f0_median(tone, sr) - 300) < 15
    assert f0_median(np.zeros(sr), sr) == 0.0                   # 무음 → 측정 불가


def test_synth_level_gates_silent_candidates():
    import numpy as np
    from src.dub import synth_level
    # 실측 사례(2026-07-08 커몬2): 무음성 합성이 피치 매칭 통과 → 정규화 증폭 잡음
    assert synth_level(np.zeros(1000, dtype=np.float32)) == 0.0
    assert synth_level(np.array([], dtype=np.float32)) == 0.0
    quiet = np.full(1000, 0.01, dtype=np.float32)
    assert synth_level(quiet) < 0.05                        # 게이트에 걸림
    loud = np.full(1000, 0.5, dtype=np.float32)
    assert synth_level(loud) == 0.5
    # int16 dtype (GPT-SoVITS 출력 형식)
    i16 = np.full(1000, 16000, dtype=np.int16)
    assert abs(synth_level(i16) - 16000/32767) < 1e-3


def test_reset_gptsovits_handle_clears_cache():
    # 퇴화 레퍼런스가 모듈 캐시를 오염 → 리셋으로 다음 호출 시 재로드 보장
    import src.dub as d
    d._GSV = object()
    d.reset_gptsovits_handle()
    assert d._GSV is None


def test_target_f0_overrides_ref_pitch_goal():
    # 피치 매칭 목표 결정: target_f0 가 있으면 ref F0 측정보다 우선(은행 ref≠영상 피치 대응).
    # 순수 규칙만 검증(합성 없이): goal = target_f0 or ref_f0.
    def resolve_goal(g, ref_f0):
        return float(g.get("target_f0", 0) or 0) or ref_f0
    assert resolve_goal({"target_f0": 405}, 492) == 405     # 영상 원본 우선
    assert resolve_goal({}, 492) == 492                     # 없으면 ref(=self-ref 시 동일)
    assert resolve_goal({"target_f0": 0}, 492) == 492       # 0은 무시


def test_needs_brighten():
    from src.dub import needs_brighten
    assert needs_brighten(2900, 4200)            # 더빙이 어두움 → 보정
    assert not needs_brighten(4300, 4200)        # 이미 충분히 밝음
    assert not needs_brighten(0, 4200)           # 측정 불가 → 스킵
    assert not needs_brighten(2900, 0)           # 목표 없음 → 스킵


def test_has_hangul():
    from src.dub import has_hangul
    assert has_hangul("マーラーヨプ떡")           # 한글 잔존
    assert not has_hangul("マーラーヨプトク")       # 순수 가타카나
    assert not has_hangul("")


def test_fix_leaked_korean_noop_without_hangul():
    # 한글 없으면 LLM 호출 없이 그대로(순수 경로) — 네트워크 의존 없음.
    from src.dub import fix_leaked_korean
    assert fix_leaked_korean("おいしくいただきます", {}) == "おいしくいただきます"


def test_strip_stage_directions():
    from src.dub import strip_stage_directions
    assert strip_stage_directions("それでは（もぐもぐ！）食べます") == "それでは食べます"
    assert strip_stage_directions("（もぐもぐ！）") == ""          # 순수 지문 → 빈 문자열(스킵)
    assert strip_stage_directions("マーラーヨプトック（ヨプキトッポッキ）") == "マーラーヨプトック"
    assert strip_stage_directions("普通の文") == "普通の文"


def test_split_for_synth():
    from src.dub import split_for_synth
    # 짧으면 그대로 1청크
    assert split_for_synth("おいしい！", 24) == ["おいしい！"]
    # 긴 문장은 구두점 단위로 쪼갬
    long = "マーラーヨプトック、ヨプキタッパル、ロゼマーラーシャングオ、クリームエビ、そしてチキンまで準備しました！"
    chunks = split_for_synth(long, 24)
    assert len(chunks) >= 2
    assert all(len(c) <= 24 * 1.6 for c in chunks)             # 강제분할 상한
    assert "".join(chunks).replace("　","") != ""              # 내용 보존
    assert split_for_synth("", 24) == []


def test_expected_synth_dur():
    from src.dub import expected_synth_dur
    assert expected_synth_dur("") == 1.0                       # 하한
    d = expected_synth_dur("あいうえおかきくけこ")            # 10자
    assert 1.0 < d < 2.5
