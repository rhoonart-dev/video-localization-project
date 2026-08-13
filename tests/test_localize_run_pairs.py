"""build_ko_ja_pairs — 검수 카드 한글 대역(8/14 사용자 요청)."""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from scripts.localize_run import build_ko_ja_pairs  # noqa: E402


def test_pairs_from_backup_and_translation():
    with tempfile.TemporaryDirectory() as tmp:
        backup = pathlib.Path(tmp) / "bk"; backup.mkdir()
        out = pathlib.Path(tmp) / "out"; out.mkdir()
        (backup / "edit_plan.json").write_text(json.dumps(
            {"layout": {"top_title": "힐링 여행인 줄 알았는데"}}, ensure_ascii=False))
        (backup / "subtitle_segments.json").write_text(json.dumps(
            [{"start_sec": 1.0, "end_sec": 2.0, "text": "너무 예뻐요"},
             {"start_sec": 3.0, "end_sec": 4.0, "text": "이건가?"}], ensure_ascii=False))
        (out / "onscreen.json").write_text(json.dumps(
            [{"text_ko": "과연 혜리는", "kind": "broadcast_telop"},
             {"text_ko": "지난 이야기", "kind": "broadcast_telop"}], ensure_ascii=False))
        tr = {"top_title_ja": "何もない田舎家で",
              "segments": [{"index": 0, "ja": "すごくきれい"}, {"index": 1, "ja": "これかな？"}],
              "telops": [{"index": 0, "use": True, "ja": "果たしてヘリは"},
                         {"index": 1, "use": False}]}
        pairs = build_ko_ja_pairs(backup, out, tr)
        assert pairs["top_title"] == {"ko": "힐링 여행인 줄 알았는데", "ja": "何もない田舎家で"}
        assert pairs["subs"][0] == {"start": 1.0, "ko": "너무 예뻐요", "ja": "すごくきれい"}
        assert len(pairs["subs"]) == 2
        # use:false 텔롭(숨김 처리분)은 대역에서도 뺀다
        assert pairs["telops"] == [{"ko": "과연 혜리는", "ja": "果たしてヘリは"}]


def test_pairs_survive_missing_files():
    with tempfile.TemporaryDirectory() as tmp:
        backup = pathlib.Path(tmp) / "no"; out = pathlib.Path(tmp) / "no2"
        pairs = build_ko_ja_pairs(backup, out, {"top_title_ja": "タイトル"})
        assert pairs["top_title"]["ja"] == "タイトル" and pairs["subs"] == [] \
            and pairs["telops"] == []
