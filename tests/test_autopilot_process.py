"""src/autopilot.py Phase 2 — QA 게이트·업로드 패키지 순수 로직 (처리 실행 없음)."""
import tempfile
from pathlib import Path

from src.autopilot import (_content_context, _dub_cmd, _srt_texts,
                           build_upload_text, final_video_for, qa_verdict)


def test_dub_cmd_safe_for_hyphen_video_id_and_config():
    # YouTube id 는 '-' 로 시작할 수 있다 → 분리형 인자면 argparse 가 옵션으로 오인.
    cmd = _dub_cmd("/venv/python", "/tmp/v.mp4", "-abc123", config_path="/cfg.yaml")
    assert "--video-id=-abc123" in cmd            # =붙임형이라 안전
    assert "--video=/tmp/v.mp4" in cmd
    assert "--level=C" in cmd
    assert "--config=/cfg.yaml" in cmd            # 커스텀 config 전파
    assert "--config=None" not in " ".join(_dub_cmd("/p", "/v", "x"))  # 미지정 시 생략


def test_srt_texts_strips_numbers_and_timecodes():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "ja.srt"
        p.write_text("1\n00:00:00,110 --> 00:00:02,110\nルーピー\n\n2\n"
                     "00:00:03,000 --> 00:00:04,000\nカモン\n", encoding="utf-8")
        assert _srt_texts(p) == ["ルーピー", "カモン"]
        assert _srt_texts(Path(tmp) / "none.srt") == []


def test_content_context_uses_measured_facts():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        pre = {"burn_frames": 0, "dialogue_segs": 1}
        # 자막 없음 → "지어내지 말 것" 가드 문구
        assert "지어내지 말 것" in _content_context(base, {"burn_frames": 0, "dialogue_segs": 0})
        (base / "ja_dub.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nルーピー\n", encoding="utf-8")
        ctx = _content_context(base, pre)
        assert "ルーピー" in ctx and "[실측]" in ctx

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
        # BJ(병기)도 final_draft.mp4 — 지도에 없으면 approve 가 "산출 영상 없음"으로
        # 죽는다(8/14 실측: 대사 없음 폴백 2건 연속)
        assert final_video_for("BJ", base).name == "final_draft.mp4"


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


def test_final_video_for_bc_route():
    # BC(캡션제거+더빙) 최종본 = 더빙 산출물
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        assert final_video_for("BC", base) is None
        (base / "final_dubbed_subbed.mp4").write_bytes(b"x")
        assert final_video_for("BC", base).name == "final_dubbed_subbed.mp4"


def test_apply_subtitle_overrides_patches_targets():
    import json
    from src.process_video import _apply_subtitle_overrides
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "translations.json").write_text(json.dumps(
            {"entries": [{"source": "하나", "target": "一"},
                         {"source": "둘", "target": "二"}]}, ensure_ascii=False))
        assert _apply_subtitle_overrides(work) == 0        # overrides 없음 = 일반 처리
        (work / "overrides.json").write_text(json.dumps(
            {"subs": {"1": "修正二", "7": "없는 인덱스"}}, ensure_ascii=False))
        assert _apply_subtitle_overrides(work) == 1
        doc = json.loads((work / "translations.json").read_text())
        assert doc["entries"][1]["target"] == "修正二"
        assert doc["entries"][0]["target"] == "一"


def test_apply_subtitle_overrides_stores_style_and_timing():
    """(8/20) style·start/end 는 entries 에 저장 — render.attach_entry_overrides 가
    이벤트로 전사한다. 검증 위반은 즉시 실패(조용한 무시 금지)."""
    import json

    import pytest
    from src.process_video import _apply_subtitle_overrides
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "translations.json").write_text(json.dumps(
            {"entries": [{"source": "하나", "target": "一"}]}, ensure_ascii=False))
        (work / "overrides.json").write_text(json.dumps(
            {"subs": {"0": {"style": {"size": 48, "y": 0.9}, "end_sec": 7.5}}},
            ensure_ascii=False))
        assert _apply_subtitle_overrides(work) == 1
        doc = json.loads((work / "translations.json").read_text())
        assert doc["entries"][0]["style"] == {"size": 48.0, "y": 0.9}
        assert doc["entries"][0]["end_sec"] == 7.5
        assert doc["entries"][0]["target"] == "一"     # ja 없이도 style 만 고칠 수 있다
        (work / "overrides.json").write_text(json.dumps(
            {"subs": {"0": {"style": {"weight": "bold"}}}}, ensure_ascii=False))
        with pytest.raises(ValueError):
            _apply_subtitle_overrides(work)


def test_apply_subtitle_overrides_stores_use_false():
    """자막 소프트 삭제(E6-0): use=false 를 entries 에 저장 — as_map(tmap)이 그 줄을
    빼 번인·ass/srt 에서 빠진다. 불리언 외에는 즉시 실패(조용한 무시 금지)."""
    import json

    import pytest
    from src.process_video import _apply_subtitle_overrides
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "translations.json").write_text(json.dumps(
            {"entries": [{"source": "하나", "target": "一"},
                         {"source": "둘", "target": "二"}]}, ensure_ascii=False))
        (work / "overrides.json").write_text(json.dumps(
            {"subs": {"0": {"use": False}}}, ensure_ascii=False))
        assert _apply_subtitle_overrides(work) == 1
        doc = json.loads((work / "translations.json").read_text())
        assert doc["entries"][0]["use"] is False and "use" not in doc["entries"][1]
        (work / "overrides.json").write_text(json.dumps(
            {"subs": {"0": {"use": "false"}}}, ensure_ascii=False))
        with pytest.raises(ValueError):
            _apply_subtitle_overrides(work)
