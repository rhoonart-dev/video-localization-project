"""src/notify.py — Slack 알림 순수 로직 (네트워크 없음)."""
from src import notify as notify_mod
from src.notify import build_digest, notify


def test_notify_noop_without_webhook():
    # 웹훅 미설정이면 조용히 False — 알림 장애가 파이프라인을 죽이면 안 된다.
    # get_secret 은 .env 파일까지 읽으므로(실키 존재 가능) 함수 자체를 치환해 격리.
    orig = notify_mod.get_secret
    notify_mod.get_secret = lambda *a, **k: None
    try:
        assert notify("테스트") is False
    finally:
        notify_mod.get_secret = orig


def test_build_digest_contents():
    counts = {"discovered": 100, "scored": 20, "pending_approval": 2, "uploaded": 1}
    top = [{"video_id": "abc", "title": "루피 쇼츠", "score": 0.71,
            "url": "https://youtube.com/shorts/abc", "view_count": 55000000}]
    pending = [{"video_id": "def", "title": "승인 대기 영상", "level_guess": "C"}]
    msg = build_digest(counts, top, pending)
    assert "루피 쇼츠" in msg and "0.71" in msg          # 후보
    assert "승인 대기 영상" in msg and "def" in msg      # 승인 큐
    assert "mark abc --state selected" in msg            # 다음 행동 안내
    assert "approve def" in msg
    assert "100" in msg                                   # 상태 집계


def test_build_digest_empty():
    msg = build_digest({}, [], [])
    assert "후보 없음" in msg and "승인 대기 없음" in msg
