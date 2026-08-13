"""src/uploader.py — 예약 슬롯·업로드 메타 순수 로직 (네트워크 없음)."""
from datetime import datetime, timezone

from src.uploader import build_upload_meta, next_publish_at


def test_next_publish_at_today_if_enough_lead():
    # UTC 07:00 = JST 16:00 → 오늘 19:00 JST(=10:00 UTC) 슬롯, 리드타임 1h 충족
    now = datetime(2026, 7, 8, 7, 0, tzinfo=timezone.utc)
    assert next_publish_at(now, set()) == "2026-07-08T10:00:00Z"


def test_next_publish_at_rolls_to_tomorrow_when_past_or_close():
    # UTC 09:30 = JST 18:30 → 오늘 슬롯까지 30분뿐(리드 1h 미달) → 내일
    now = datetime(2026, 7, 8, 9, 30, tzinfo=timezone.utc)
    assert next_publish_at(now, set()) == "2026-07-09T10:00:00Z"
    # 이미 지난 뒤(JST 20:00)도 내일
    now2 = datetime(2026, 7, 8, 11, 0, tzinfo=timezone.utc)
    assert next_publish_at(now2, set()) == "2026-07-09T10:00:00Z"


def test_next_publish_at_skips_taken_slots():
    # 하루 1편 페이스: 이미 잡힌 슬롯은 건너뛰어 연속 날짜로 분산
    now = datetime(2026, 7, 8, 7, 0, tzinfo=timezone.utc)
    taken = {"2026-07-08T10:00:00Z", "2026-07-09T10:00:00Z"}
    assert next_publish_at(now, taken) == "2026-07-10T10:00:00Z"


def test_build_upload_meta_contents():
    draft = {"title_candidates": ["ルーピー登場!", "예비 제목"],
             "description": "説明\n\n© IP", "hashtags": ["#ルーピー"],
             "tags": ["loopy", "japan"]}
    row = {"video_id": "abc", "title": "원제"}
    body = build_upload_meta(draft, row, route="C", publish_at="2026-07-09T10:00:00Z",
                             ucfg={"category_id": "24", "made_for_kids": False})
    sn, st = body["snippet"], body["status"]
    assert sn["title"] == "ルーピー登場!"              # 1안 자동 선택
    assert "説明" in sn["description"]
    assert sn["tags"] == ["loopy", "japan"]
    assert sn["categoryId"] == "24"
    assert sn["defaultAudioLanguage"] == "ja"          # route C = 더빙
    assert st["privacyStatus"] == "private"            # 예약 공개는 private 전제
    assert st["publishAt"] == "2026-07-09T10:00:00Z"
    assert st["selfDeclaredMadeForKids"] is False


def test_build_upload_meta_fallbacks():
    # 메타 초벌이 없어도(빈 draft) 원제로 업로드 가능해야 한다
    body = build_upload_meta({}, {"video_id": "x", "title": "원제만 있음"}, route="A",
                             publish_at="2026-07-09T10:00:00Z", ucfg={})
    assert body["snippet"]["title"] == "원제만 있음"
    assert "defaultAudioLanguage" not in body["snippet"]   # A=원본 오디오(한국어) 유지


def test_build_upload_meta_privacy_modes():
    """관제 3택(8/14): 예약=private+publishAt · 비공개/일부공개=publishAt 없음."""
    from src.uploader import build_upload_meta
    draft = {"title_candidates": ["タイトル"], "description": "説明", "tags": ["t"]}
    row = {"video_id": "v1", "title": "원제"}
    b = build_upload_meta(draft, row, "C", "2026-08-15T10:00:00Z", {}, privacy="private")
    assert b["status"]["privacyStatus"] == "private" and b["status"]["publishAt"]
    b2 = build_upload_meta(draft, row, "B", None, {}, privacy="unlisted")
    assert b2["status"]["privacyStatus"] == "unlisted" and "publishAt" not in b2["status"]
    b3 = build_upload_meta(draft, row, "B", None, {}, privacy="private")
    assert b3["status"]["privacyStatus"] == "private" and "publishAt" not in b3["status"]
    # 예약인데 unlisted 를 넘겨도 private 으로 강제(YouTube 규약)
    b4 = build_upload_meta(draft, row, "B", "2026-08-15T10:00:00Z", {}, privacy="unlisted")
    assert b4["status"]["privacyStatus"] == "private"
