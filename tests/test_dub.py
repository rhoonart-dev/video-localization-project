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


def test_pacing_plan_natural_within_cap_no_stretch():
    # 원본 보컬 제거 더빙 = 입싱크 불필요 → cap(다음 대사 침범선) 안이면 자연 속도.
    from src.dub import pacing_plan
    speed, out = pacing_plan(natural=1.9, cap=3.5, max_speedup=1.35)
    assert speed == 1.0 and out == 1.9                  # 슬롯보다 길어도 그대로
    speed, out = pacing_plan(natural=3.3, cap=3.2, max_speedup=1.35)
    assert 1.0 < speed <= 1.35 and abs(out - 3.2) < 1e-9


def test_pacing_plan_overflow_clamped_then_truncate():
    from src.dub import pacing_plan
    # cap 대비 크게 초과 → max_speedup 클램프(잘림은 _fit_audio 캡이 처리)
    speed, out = pacing_plan(natural=12.0, cap=7.3, max_speedup=1.35)
    assert speed == 1.35 and abs(out - 12.0 / 1.35) < 1e-6
    # 경계/무효 입력
    assert pacing_plan(0, 5, 1.35) == (1.0, 0)
    assert pacing_plan(5, 0, 1.35) == (1.0, 5)


def test_char_budget_for_dub_translation():
    from src.dub import char_budget
    # 슬롯 초수 × 발화속도(자/초) — 최소 하한 보장
    assert char_budget(7.3, 5.5) == 40
    assert char_budget(0.5, 5.5) == 8                    # 짧아도 최소 8자
    assert char_budget(2.7, 5.5) == 14


def test_retime_events_to_actual_durations():
    from src.dub import retime_events
    events = [{"start": 0.0, "end": 7.3, "text": "a"},
              {"start": 7.3, "end": 10.0, "text": "b"},
              {"start": 10.0, "end": 11.3, "text": "c"}]
    # 실제 발화 길이(자연 속도) — c 는 다음 세그 없음 → 그대로 연장
    out = retime_events(events, durs=[6.0, 2.0, 1.9], guard=0.05)
    assert out[0]["end"] == 6.0                          # 짧아진 발화 → 자막도 축소
    assert out[1]["end"] == 9.3                          # 7.3+2.0
    assert abs(out[2]["end"] - 11.9) < 1e-9              # 마지막은 자유 연장
    # 다음 세그 시작을 넘으면 guard 만큼 앞에서 클램프
    out2 = retime_events(events, durs=[8.0, 2.0, 1.0], guard=0.05)
    assert abs(out2[0]["end"] - (7.3 - 0.05)) < 1e-9


# ── 자가개선: ASR 백체크 (2026-07-21) ─────────────────────────────────────
from src.dub import backcheck_summary, cer, levenshtein, norm_for_cer, synthesize_checked

BC_CFG = {"dub": {"language": "ja",
                  "backcheck": {"enabled": True, "max_cer": 0.3,
                                "retries": 2, "fail_cer": 0.5}}}


def test_levenshtein_and_cer_basics():
    assert levenshtein("abc", "abc") == 0
    assert levenshtein("abc", "axc") == 1
    assert levenshtein("", "ab") == 2
    assert cer("abcd", "abcd") == 0.0
    assert cer("abcd", "abxd") == 0.25
    assert cer("", "") == 0.0
    assert cer("", "x") == 1.0                        # ref 없음 + 환각 출력


def test_norm_for_cer_fallback_absorbs_punct_space_case():
    # pyopenjtalk 미설치 환경 폴백: NFKC + 기호/공백 제거 + 소문자화
    assert norm_for_cer("ガンバレ、センパイ!", "ja") == norm_for_cer("ガンバレ センパイ", "ja")
    assert norm_for_cer("Hello, World!", "en") == "helloworld"
    assert norm_for_cer("", "ja") == ""


def test_synthesize_checked_disabled_passthrough():
    cfg = {"dub": {"backcheck": {"enabled": False}}}
    data, c = synthesize_checked("テスト", cfg, synth_fn=lambda: b"WAV",
                                 asr_fn=lambda d: (_ for _ in ()).throw(AssertionError("ASR 호출 금지")))
    assert data == b"WAV" and c is None


def test_synthesize_checked_retries_and_picks_best():
    # 1차 합성 CER 1.0(오인식) → 재합성에서 정답 → 더 나은 후보 채택
    outs = iter([b"BAD", b"GOOD", b"GOOD2"])
    asr = {b"BAD": "全然違う文", b"GOOD": "ガンバレセンパイ", b"GOOD2": "ガンバレセンパイ"}
    data, c = synthesize_checked("ガンバレセンパイ", BC_CFG, lang="ja",
                                 synth_fn=lambda: next(outs), asr_fn=lambda d: asr[d])
    assert data == b"GOOD" and c == 0.0


def test_synthesize_checked_asr_failure_does_not_kill_dub():
    def boom(d):
        raise RuntimeError("asr down")
    data, c = synthesize_checked("テスト", BC_CFG, lang="ja",
                                 synth_fn=lambda: b"WAV", asr_fn=boom)
    assert data == b"WAV" and c is None               # 백체크만 생략, 더빙 계속


def test_backcheck_summary_counts_failures():
    segs = [{"backcheck_cer": 0.0}, {"backcheck_cer": 0.6},
            {"backcheck_cer": None}, {"text": "no-key"}]
    s = backcheck_summary(segs, fail_cer=0.5)
    assert s["checked"] == 2 and s["failed"] == 1
    assert s["cer_max"] == 0.6 and abs(s["cer_avg"] - 0.3) < 1e-9
    empty = backcheck_summary([], fail_cer=0.5)
    assert empty["checked"] == 0 and empty["failed"] == 0


def test_apply_dub_overrides_by_prefilter_idx():
    from src.dub import apply_dub_overrides
    events = [{"start": 1.0, "end": 2.0, "text": "一"},
              {"start": 3.0, "end": 4.0, "text": ""},      # 지문 제거로 비었던 줄
              {"start": 5.0, "end": 6.0, "text": "三"}]
    out, n = apply_dub_overrides(events, {"subs": {"1": "補充", "2": {"ja": "修正三"},
                                                   "9": "없는 인덱스", "x": "무시"}})
    assert n == 2
    assert out[1]["text"] == "補充" and out[2]["text"] == "修正三"
    assert events[1]["text"] == ""                          # 원본 불변(순수)
    same, n0 = apply_dub_overrides(events, {})
    assert n0 == 0 and same[0]["text"] == "一"


def test_strip_non_lexical_drops_babble_keeps_words():
    from src.dub import strip_non_lexical
    # 옹알이 토큰만 제거, 진짜 말은 유지 (8/14 실측 사례)
    assert strip_non_lexical("끙끙끙끙야 아지아지야 오 너무 예뻐") == "오 너무 예뻐"
    # 진짜 단어의 반복(노래 가사)은 옹알이가 아니다
    assert strip_non_lexical("배고파 배고파 배고파 배고파") == "배고파 배고파 배고파 배고파"
    # 전부 옹알이면 빈 문자열 = 이 구간은 더빙·자막 대상 아님
    assert strip_non_lexical("음냐음냐 끄으으으응") == ""
    # 한계(의도): 2연속 반복('끄으응')은 안 잡는다 — 사전 없이 잡으면 '바나나' 같은
    # 실제 단어가 오탐된다. 단독이면 어차피 _is_dialogue 3음절 규칙으로 걸러질 것 많음.
    assert strip_non_lexical("바나나 먹었어요") == "바나나 먹었어요"
    assert strip_non_lexical("야!") == ""
    assert strip_non_lexical("") == ""
    # 짧지만 성립하는 한마디는 남는다
    assert strip_non_lexical("맛있어요") == "맛있어요"
    assert strip_non_lexical("아아아아 잘 먹겠습니다") == "잘 먹겠습니다"


# ── 줄 스타일·타이밍 오버라이드(8/20 — docs/subtitle-style-overrides.md) ──

def test_apply_dub_overrides_style_and_timing():
    from src.dub import apply_dub_overrides
    events = [{"idx": 0, "start": 1.0, "end": 2.0, "text": "一"},
              {"idx": 1, "start": 3.0, "end": 4.0, "text": "二"}]
    out, n = apply_dub_overrides(events, {"subs": {
        "0": {"style": {"size": 64, "color": "#ffdd00"}, "end_sec": 2.8},
        "1": {"ja": "修正二", "start_sec": 3.2}}})
    assert n == 2
    assert out[0]["style"] == {"size": 64.0, "color": "#FFDD00"}
    assert out[0]["end"] == 2.8 and out[0]["end_fixed"] is True   # retime 이 안 덮는다
    assert out[1]["start"] == 3.2 and "end_fixed" not in out[1]   # end 미지정 = retime 대상
    assert events[0]["end"] == 2.0                                # 원본 불변(순수)


def test_apply_dub_overrides_rejects_bad_style():
    import pytest
    from src.dub import apply_dub_overrides
    events = [{"idx": 0, "start": 1.0, "end": 2.0, "text": "一"}]
    with pytest.raises(ValueError):                               # 모르는 style 키 거절
        apply_dub_overrides(events, {"subs": {"0": {"style": {"fontsize": 12}}}})
    with pytest.raises(ValueError):                               # end ≤ start
        apply_dub_overrides(events, {"subs": {"0": {"start_sec": 5, "end_sec": 4}}})


def test_retime_events_preserves_user_fixed_end():
    from src.dub import retime_events
    events = [{"start": 0.0, "end": 2.0, "text": "a", "end_fixed": True},
              {"start": 3.0, "end": 4.0, "text": "b"},
              {"start": 6.0, "end": 7.0, "text": "c"}]
    out = retime_events(events, [5.0, 1.5, 2.0])
    assert out[0]["end"] == 2.0                        # 사용자 값 우선 — 실측 5s 를 무시
    assert out[1]["end"] == 4.5                        # 일반 세그는 실측 재정렬(3.0+1.5)
    assert out[2]["end"] == 8.0                        # 마지막 세그 자유 연장


def test_build_dub_pairs_and_actual_end_update():
    from src.dub import build_dub_pairs, update_pairs_actual_ends
    segs = [{"start": 1.0, "end": 2.0, "text": "하나"},
            {"start": 3.0, "end": 4.0, "text": "（지문）"}]
    events = [{"idx": 0, "start": 1.0, "end": 2.8, "end_fixed": True, "text": "一",
               "style": {"color": "#FF0000"}},
              {"idx": 1, "start": 3.0, "end": 4.0, "text": ""}]
    pairs = build_dub_pairs(segs, events)
    assert pairs["subs"][0] == {"idx": 0, "start": 1.0, "end": 2.8, "end_actual": False,
                                "ko": "하나", "ja": "一",
                                "style": {"color": "#FF0000"}, "end_fixed": True}
    assert pairs["subs"][1]["end_actual"] is False and "style" not in pairs["subs"][1]
    # retime 후: 살아남은 이벤트(idx 0)만 실측 end 로 갱신 — 필터된 idx 1 은 계획값 유지
    updated = update_pairs_actual_ends(pairs, [{"idx": 0, "start": 1.0, "end": 2.8,
                                                "end_fixed": True, "text": "一"}])
    assert updated["subs"][0]["end_actual"] is True
    assert updated["subs"][1]["end_actual"] is False
    assert pairs["subs"][0]["end_actual"] is False     # 원본 불변(순수)


# ── 자막·대사 소프트 삭제(E6-0 — subs use:false) ─────────────────────────

def test_apply_dub_overrides_use_false_and_pairs_skip():
    import pytest
    from src.dub import apply_dub_overrides, build_dub_pairs
    events = [{"idx": 0, "start": 1.0, "end": 2.0, "text": "一"},
              {"idx": 1, "start": 3.0, "end": 4.0, "text": "二"}]
    out, n = apply_dub_overrides(events, {"subs": {"0": {"use": False}}})
    assert n == 1 and out[0]["use"] is False and "use" not in out[1]
    assert "use" not in events[0]                        # 원본 불변(순수)
    with pytest.raises(ValueError):                      # 불리언 외 거절(조용한 무시 금지)
        apply_dub_overrides(events, {"subs": {"0": {"use": 0}}})
    # 다음 카드 pairs 에서 빠진다 — 남은 줄의 idx(필터 전 순번)는 유지
    segs = [{"start": 1.0, "end": 2.0, "text": "하나"},
            {"start": 3.0, "end": 4.0, "text": "둘"}]
    pairs = build_dub_pairs(segs, out)
    assert [r["idx"] for r in pairs["subs"]] == [1]
    # 호출부 필터(빈 대사와 같은 지점) 판정 — SRT(합성 드라이버)·번인·retime 이 함께 뺀다
    kept = [e for e in out if e.get("use") is not False and e["text"].strip()]
    assert [e["idx"] for e in kept] == [1]
