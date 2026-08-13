"""convert_short v2 순수부 테스트 — 8/13 실물 실패 세 가지의 회귀 방지."""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.convert_short import (ass_time_to_sec, audio_graph, build_ja_ass,  # noqa: E402
                               duck_expr, parse_ass_events, sec_to_ass_time, wrap_jp)

ASS = """[Script Info]
ScriptType: v4.00+

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.50,0:00:04.00,TTS,,0,0,0,,{\\an2}第2ラウンドが始まり
Dialogue: 0,0:00:04.00,0:00:07.25,TTS,,0,0,0,,連続\\N正解していく
Dialogue: 0,0:00:00.00,0:00:00.00,TTS,,0,0,0,,{\\an2}
"""


def test_ass_time_roundtrip():
    assert ass_time_to_sec("0:00:12.34") == 12.34
    assert ass_time_to_sec("1:02:03.05") == 3723.05
    assert sec_to_ass_time(12.34) == "0:00:12.34"
    assert sec_to_ass_time(0) == "0:00:00.00"
    assert sec_to_ass_time(3723.999) == "1:02:04.00"   # 반올림 자리올림


def test_parse_ass_events_strips_tags_and_sorts():
    ev = parse_ass_events(ASS)
    assert len(ev) == 2                                  # 빈 텍스트 이벤트 제외
    assert ev[0]["start"] == 1.5 and ev[0]["end"] == 4.0
    assert ev[0]["text"] == "第2ラウンドが始まり"          # {\an2} 태그 제거
    assert ev[1]["text"] == "連続 正解していく"            # \N → 공백


def test_wrap_jp_prevents_overflow():
    """8/13 실측 ①: 제목이 줄바꿈 없이 화면 밖으로 나갔다."""
    t = wrap_jp("連続正解に大喜びしたのもつかの間、自分の問題で間違えた", max_chars=14, max_lines=2)
    lines = t.split("\\N")
    assert len(lines) == 2 and all(len(x) <= 14 for x in lines)   # 27자 → 딱 2줄, 잘림 없음
    over = wrap_jp("あ" * 40, max_chars=14, max_lines=2)          # 40자 → 넘침
    ls = over.split("\\N")
    assert len(ls) == 2 and all(len(x) <= 14 for x in ls) and ls[-1].endswith("…")
    assert wrap_jp("短い") == "短い"


def test_build_ja_ass_layout():
    """8/13 실측 ②: 나레이션 전문이 한 덩어리로 화면을 덮었다 — cue 별 이벤트."""
    nar = [{"start": 1.0, "end": 4.0, "text": "第2ラウンドが始まり"},
           {"start": 4.0, "end": 8.0, "text": "アンカーのヘリが自分の問題で間違えた"}]
    dlg = [{"start": 2.0, "end": 3.0, "text": "最初のセリフ"}]
    s = build_ja_ass("日本語タイトルです", dlg, nar, 1080, 1920, 12.0)
    assert s.count("Dialogue:") == 4                     # 제목1 + 나레이션2 + 대사1
    assert "JTitle" in s and "JNarr" in s and "JDlg" in s
    assert "0:00:01.00,0:00:04.00,JNarr" in s            # cue 타이밍 보존
    # 제목은 영상 전체 구간
    assert "Dialogue: 0,0:00:00.00,0:00:12.00,JTitle" in s
    # 제목 없으면 제목 이벤트 없음
    assert build_ja_ass(None, dlg, nar, 1080, 1920, 12.0).count("Dialogue:") == 3


def test_duck_and_mix_graph():
    """8/13 실측 ③ 후속: 원본 오디오는 cue 구간만 덕킹, TTS 는 cue 시작에 배치."""
    spans = [(1.5, 4.0), (10.0, 12.5)]
    d = duck_expr(spans, 0.3)
    assert d.startswith("volume=0.3:enable=") and "between(t,1.500,4.000)" in d
    assert duck_expr([]) == ""
    g = audio_graph(2, spans, 0.3)
    assert "adelay=1500|1500" in g and "adelay=10000|10000" in g
    assert "amix=inputs=3" in g and g.endswith("[aout]")
    g0 = audio_graph(0, [], 0.3)
    assert "amix" not in g0 and g0.endswith("[aout]")    # cue 없음 — 원본 그대로
