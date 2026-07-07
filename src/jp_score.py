"""일본 적합도 스코어링 — 원 채널 '공개 신호' 기반 (autopilot Phase 1).

src/select.py 는 *자기 채널* analytics CSV(국가별 시청 비중) 기반인 반면, 이 모듈은
아직 우리 채널에 없는 *원 채널* 영상을 공개 신호만으로 평가한다:
  정량: 조회수(log 정규화) · 좋아요율 · 댓글 일본어 비율(가나 문자)
  정성: LLM(Gemini) — 일본 밈 적합도 + 언어 의존도(낮을수록 현지화 쉬움)
없는 신호(댓글 비활성, LLM 실패)는 가중치를 재정규화해 흡수한다.

순수 함수(스코어 결합·파싱)와 LLM 호출(llm_score_batch)을 분리 — 전자만 테스트.
"""
from __future__ import annotations

import json
import math
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from typing import Any, Optional  # noqa: E402

from engine.common import get_logger  # noqa: E402

log = get_logger("jp_score")

# 히라가나(3040-309F) + 가타카나(30A0-30FF). 한자는 중국어와 공유라 제외 — 가나가 일본어 확정 신호.
_KANA_RE = re.compile(r"[぀-ヿ]")


# ── 순수: 언어/정량 신호 ──────────────────────────────────────────────────
def has_kana(text: str) -> bool:
    return bool(_KANA_RE.search(text or ""))


def kana_ratio(texts: list[str]) -> Optional[float]:
    """댓글 표본 중 가나 포함 비율. 표본 없으면 None(신호 없음)."""
    if not texts:
        return None
    return round(sum(1 for t in texts if has_kana(t)) / len(texts), 4)


def log_norm(value: float, max_value: float) -> float:
    """조회수처럼 롱테일 분포 값의 log 정규화(0~1)."""
    if not max_value or max_value <= 0 or not value or value <= 0:
        return 0.0
    return min(1.0, math.log1p(value) / math.log1p(max_value))


def like_norm(likes: float, views: float, good_ratio: float = 0.05) -> float:
    """좋아요율 정규화 — 5%(쇼츠 기준 매우 좋음)를 1.0 상한으로."""
    if not views or views <= 0 or not likes or likes <= 0:
        return 0.0
    return min(1.0, (likes / views) / good_ratio)


def combine_scores(signals: dict[str, Optional[float]], weights: dict[str, float]) -> float:
    """신호 가중합. None 신호는 제외하고 남은 가중치로 재정규화(합=1 유지)."""
    avail = {k: v for k, v in signals.items() if v is not None}
    if not avail:
        return 0.0
    wsum = sum(weights.get(k, 0.0) for k in avail)
    if wsum <= 0:
        return 0.0
    return round(sum(weights.get(k, 0.0) * v for k, v in avail.items()) / wsum, 6)


def llm_component(item: dict[str, Any]) -> Optional[float]:
    """LLM 세부점수 → 0~1 성분. jp_fit 높을수록·언어 의존 낮을수록 좋음."""
    if "jp_fit" not in item:
        return None
    fit = max(0.0, min(10.0, float(item.get("jp_fit", 0))))
    dep = max(0.0, min(10.0, float(item.get("language_dependence", 5))))
    return round((fit + (10.0 - dep)) / 20.0, 4)


def parse_llm_array(content: str) -> list[dict[str, Any]]:
    """LLM 응답에서 JSON 배열 파싱 (src/metadata.py parse_llm_object 의 배열판)."""
    fence = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
    if fence:
        content = fence.group(1)
    start, end = content.find("["), content.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"LLM 응답에 JSON 배열 없음: {content[:200]!r}")
    data = json.loads(content[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("JSON 배열이 아님")
    return data


# ── LLM 호출 (lazy — engine.llm) ─────────────────────────────────────────
_SYSTEM = """너는 한국 캐릭터 쇼츠의 일본 시장 적합성을 평가하는 콘텐츠 애널리스트다.
잔망루피(핑크 비버 캐릭터, 한국 밈 문화 기반, 일본에서도 인지도 상승 중) 쇼츠를
일본 전용 채널에 현지화(자막/캡션교체/더빙)해 올릴 후보로 평가한다.
평가 기준:
- jp_fit(0~10): 일본 시청자 반응 기대치. 캐릭터 귀여움·사운드·표정 중심(언어 무관 유머),
  일본에서 통하는 밈 코드, 먹방/ASMR 등 일본 인기 장르면 높게.
- language_dependence(0~10): 한국어 이해가 필요한 정도. 한국어 말장난·자막드립·
  한국 시사 맥락 의존이면 높게(=현지화 어려움), 비언어적이면 낮게.
- level_guess(A|B|C): A=화면 텍스트 없음(자막만), B=화면 한국어 텍스트 있어 보임(캡션 교체),
  C=대사 있어 보임(더빙 필요). 제목만으로 추정이므로 확신 없으면 A.
반드시 JSON 배열만 출력: [{"video_id","jp_fit","language_dependence","level_guess","reason"}].
reason 은 한국어 한 문장."""


def llm_score_batch(items: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """제목 배치를 LLM 으로 평가 → {video_id: {jp_fit, language_dependence, level_guess, reason}}.

    실패 시 빈 dict(호출자가 신호 없음으로 처리) — 파이프라인을 멈추지 않는다.
    """
    from engine.llm import complete
    payload = [{"video_id": it["video_id"], "title": it.get("title", ""),
                "duration_sec": it.get("duration"), "view_count": it.get("view_count")}
               for it in items]
    user = ("다음 쇼츠 후보들을 평가하라. 입력:\n"
            + json.dumps(payload, ensure_ascii=False)
            + "\n출력: 같은 video_id 를 가진 JSON 배열만.")
    max_tokens = int(config.get("translate", {}).get("max_tokens", 4096))
    try:
        raw = complete(_SYSTEM, user, config, max_tokens=max_tokens)
        return {d["video_id"]: d for d in parse_llm_array(raw) if isinstance(d, dict) and d.get("video_id")}
    except Exception as e:  # LLM 장애가 스캔 전체를 죽이지 않게
        log.warning("LLM 스코어링 실패(%s) → 정량 신호만 사용", e)
        return {}
