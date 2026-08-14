"""build_ko_ja_pairs·apply_overrides — 검수 카드 한글 대역 + 반려-수정 재렌더(8/14)."""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from scripts.localize_run import apply_overrides, build_ko_ja_pairs  # noqa: E402


def test_pairs_from_backup_and_translation():
    with tempfile.TemporaryDirectory() as tmp:
        backup = pathlib.Path(tmp) / "bk"; backup.mkdir()
        out = pathlib.Path(tmp) / "out"; out.mkdir()
        (backup / "edit_plan.json").write_text(json.dumps(
            {"layout": {"top_title": "힐링 여행인 줄 알았는데"}}, ensure_ascii=False))
        (backup / "subtitle_segments.json").write_text(json.dumps(
            [{"start_sec": 1.0, "end_sec": 2.0, "text": "너무 예뻐요"},
             {"start_sec": 3.0, "end_sec": 4.0, "text": "이건가?"}], ensure_ascii=False))
        (backup / "checkpoint_resources.json").write_text(json.dumps(
            {"tts_cue_files": [{"path": "x.mp3", "cue_index": 0,
                                "cue": {"text": "과연 결과는?"}}]}, ensure_ascii=False))
        (out / "onscreen.json").write_text(json.dumps(
            [{"text_ko": "과연 혜리는", "kind": "broadcast_telop"},
             {"text_ko": "지난 이야기", "kind": "broadcast_telop"}], ensure_ascii=False))
        tr = {"top_title_ja": "何もない田舎家で",
              "segments": [{"index": 0, "ja": "すごくきれい"}, {"index": 1, "ja": "これかな？"}],
              "tts_cues": [{"index": 0, "ja": "果たして結果は？"}],
              "telops": [{"index": 0, "use": True, "ja": "果たしてヘリは"},
                         {"index": 1, "use": False}]}
        pairs = build_ko_ja_pairs(backup, out, tr)
        assert pairs["top_title"] == {"ko": "힐링 여행인 줄 알았는데", "ja": "何もない田舎家で"}
        # idx(8/14 반려-수정): 오버라이드 좌표 — translation index 필드와 같아야 한다
        assert pairs["subs"][0] == {"idx": 0, "start": 1.0, "ko": "너무 예뻐요", "ja": "すごくきれい"}
        assert len(pairs["subs"]) == 2 and pairs["subs"][1]["idx"] == 1
        assert pairs["tts"] == [{"idx": 0, "ko": "과연 결과는?", "ja": "果たして結果は？"}]
        # use:false 텔롭(숨김 처리분)은 대역에서도 뺀다 — 남은 항목은 원래 idx 를 유지
        assert pairs["telops"] == [{"idx": 0, "ko": "과연 혜리는", "ja": "果たしてヘリは"}]


def test_pairs_survive_missing_files():
    with tempfile.TemporaryDirectory() as tmp:
        backup = pathlib.Path(tmp) / "no"; out = pathlib.Path(tmp) / "no2"
        pairs = build_ko_ja_pairs(backup, out, {"top_title_ja": "タイトル"})
        assert pairs["top_title"]["ja"] == "タイトル" and pairs["subs"] == [] \
            and pairs["tts"] == [] and pairs["telops"] == []


def test_apply_overrides_merges_by_index():
    tr = {"youtube_title_ja": "旧タイトル", "top_title_ja": "旧\n上部", "description_ja": "旧説明",
          "segments": [{"index": 0, "ja": "一"}, {"index": 1, "ja": "二"}],
          "tts_cues": [{"index": 0, "ja": "ナレ"}],
          "telops": [{"index": 0, "use": True, "ja": "テロップ"}]}
    ov = {"youtube_title_ja": "新タイトル",
          "subs": {"1": "修正二", "9": "없는 인덱스는 무시"},
          "tts": {"0": {"ja": "新ナレ"}},
          "telops": {"0": {"ja": "新テロップ", "use": False}}}
    out = apply_overrides(tr, ov)
    assert out["youtube_title_ja"] == "新タイトル"
    assert out["top_title_ja"] == "旧\n上部"                 # 안 고친 필드는 그대로
    assert out["segments"][1]["ja"] == "修正二"
    assert out["tts_cues"][0]["ja"] == "新ナレ"
    assert out["telops"][0] == {"index": 0, "use": False, "ja": "新テロップ"}
    # 원본 불변(순수) — 렌더 정본은 호출부가 재기록으로만 바꾼다
    assert tr["youtube_title_ja"] == "旧タイトル" and tr["segments"][1]["ja"] == "二"


def test_apply_overrides_ignores_blank_and_bad_keys():
    tr = {"segments": [{"index": 0, "ja": "一"}]}
    out = apply_overrides(tr, {"youtube_title_ja": "  ",
                               "subs": {"abc": "x", "0": "  "}})
    assert "youtube_title_ja" not in out and out["segments"][0]["ja"] == "一"
