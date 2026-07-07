"""src/scout.py — 채널 스카우트 순수 로직 (네트워크 없음)."""
from src.scout import (parse_iso8601_duration, parse_ytdlp_flat_lines,
                       shorts_playlist_id, within_duration)


def test_parse_iso8601_duration():
    assert parse_iso8601_duration("PT14S") == 14.0
    assert parse_iso8601_duration("PT1M3S") == 63.0
    assert parse_iso8601_duration("PT2M") == 120.0
    assert parse_iso8601_duration("PT1H2M3S") == 3723.0
    assert parse_iso8601_duration("") is None
    assert parse_iso8601_duration("garbage") is None


def test_shorts_playlist_id_from_channel_id():
    # UC<suffix> → UUSH<suffix> (Shorts 전용 자동 플레이리스트, 미문서화)
    assert shorts_playlist_id("UChIgH6NybaNC_mkp4-gbX3A") == "UUSHhIgH6NybaNC_mkp4-gbX3A"
    try:
        shorts_playlist_id("XX123")
        assert False, "UC 로 시작하지 않으면 ValueError"
    except ValueError:
        pass


def test_parse_ytdlp_flat_lines():
    lines = [
        "abc123\t루피 쇼츠 1\t67000",
        "def456\t루피 쇼츠 2\tNA",       # view_count 없음
        "",                                # 빈 줄 무시
        "broken-line-without-tabs",        # 형식 깨짐 무시
    ]
    rows = parse_ytdlp_flat_lines(lines)
    assert len(rows) == 2
    assert rows[0]["video_id"] == "abc123"
    assert rows[0]["view_count"] == 67000
    assert rows[0]["url"] == "https://www.youtube.com/shorts/abc123"
    assert rows[1]["view_count"] is None


def test_within_duration():
    assert within_duration(30.0, 3, 183)
    assert not within_duration(2.0, 3, 183)        # 너무 짧음(인트로 조각 등)
    assert not within_duration(200.0, 3, 183)      # 3분 초과 → Shorts 아님
    assert within_duration(None, 3, 183)           # 길이 미상(ytdlp flat)은 통과
