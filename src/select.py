"""콘텐츠 선별 분석 (C-2) — 일본 진출 우선순위 산출.

data/analytics/*.csv (영상별 조회·retention·국가별 시청)를 읽어:
  점수 = 일본 시청 비중(최우선) + retention + 조회수(정규화) 가중합
  + 제목 키워드로 등급(A/B/C) '추정'.
→ outputs/selection_ranked.csv (배치 순서 포함).

순수 stdlib(csv) — 의존성 없이 동작·테스트 가능. 컬럼 매핑은 config.select.columns.
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pathlib import Path  # noqa: E402
from typing import Any, Optional  # noqa: E402

from engine.common import ensure_dir, get_logger, load_config, resolve_path  # noqa: E402

log = get_logger("select")


# ── 순수 헬퍼 ─────────────────────────────────────────────────────────────
def estimate_level(title: str, level_keywords: dict[str, list[str]]) -> str:
    """제목 키워드로 등급 추정. 우선순위 A>B>C(자막중심부터)."""
    low = (title or "").lower()
    for level in ("A", "B", "C"):
        for kw in level_keywords.get(level, []):
            if str(kw).lower() in low:
                return level
    return "B"  # 불명확하면 중간(번인 가능성)


def to_unit(value: Any) -> float:
    """0~1 또는 0~100 스케일 값을 0~1 로 정규화."""
    try:
        v = float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return 0.0
    if v > 1.0:
        v = v / 100.0
    return max(0.0, min(1.0, v))


def minmax(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def composite_score(jp_share: float, retention: float, views_norm: float,
                    weights: dict[str, float]) -> float:
    return round(weights.get("jp_share", 0.5) * jp_share
                 + weights.get("retention", 0.3) * retention
                 + weights.get("views", 0.2) * views_norm, 6)


# ── 분석 ──────────────────────────────────────────────────────────────────
def _to_float(x: Any) -> float:
    try:
        return float(str(x).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return 0.0


def rank_rows(rows: list[dict[str, str]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """원시 CSV 행 → 점수·등급·배치순서가 붙은 정렬된 행."""
    scfg = config.get("select", {})
    cols = scfg.get("columns", {})
    weights = scfg.get("weights", {})
    kw = scfg.get("level_keywords", {})

    views_raw = [_to_float(r.get(cols.get("views", "views"), 0)) for r in rows]
    views_norm = minmax(views_raw)

    out = []
    for r, vn in zip(rows, views_norm):
        title = r.get(cols.get("title", "title"), "")
        jp = to_unit(r.get(cols.get("jp_share", "jp_view_share"), 0))
        ret = to_unit(r.get(cols.get("retention", "average_view_percentage"), 0))
        out.append({
            "video_id": r.get(cols.get("video_id", "video_id"), ""),
            "title": title,
            "estimated_level": estimate_level(title, kw),
            "jp_share": round(jp, 4),
            "retention": round(ret, 4),
            "views": int(_to_float(r.get(cols.get("views", "views"), 0))),
            "score": composite_score(jp, ret, vn, weights),
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    for i, row in enumerate(out, 1):
        row["batch_order"] = i
    return out


def select(analytics_csv: str, config: dict[str, Any], out_path: Optional[str] = None) -> Path:
    with open(analytics_csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    ranked = rank_rows(rows, config)

    out = Path(out_path) if out_path else resolve_path(
        f"{config['paths']['outputs_dir']}/selection_ranked.csv")
    ensure_dir(out.parent)
    fields = ["batch_order", "video_id", "title", "estimated_level",
              "jp_share", "retention", "views", "score"]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in ranked:
            w.writerow({k: row[k] for k in fields})
    log.info("선별 완료: %d편 → %s (등급은 '추정'; 사람이 수정 가능)", len(ranked), out)
    return out


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="일본 진출 우선순위 선별(등급 추정)")
    p.add_argument("--analytics", required=True, help="data/analytics/*.csv")
    p.add_argument("--config", default=None)
    p.add_argument("--out", default=None)
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    select(args.analytics, load_config(args.config), out_path=args.out)


if __name__ == "__main__":
    main()
