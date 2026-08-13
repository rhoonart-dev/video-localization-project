#!/usr/bin/env python3
"""ai-video job 디렉토리 하나를 일본어로 현지화한다 (PLAN.md §2 파이프라인).

    L0 백업 → L2 텔롭 추출(Gemini 영상) → L1 교정-번역(Gemini 텍스트)
    → L3 적용(job 데이터 파일 교체 + 텔롭 ass) → L4 재렌더 + 텔롭 번인 → L5 메타데이터

원칙:
- 번역의 입력은 항상 `localize_backup_ko/` 의 한국어 원본이다 — 재실행해도 이중 번역이 없다.
- 렌더 입력이 되는 파일은 **checkpoint_story.json(제목)·subtitle_segments.json(대사)·
  checkpoint_resources.json(TTS)** 이고 edit_plan.json 은 발행(DB 제목 조회)용으로 함께 갱신한다
  (SPIKE_RESULTS.md §설계수정-1).
- 재렌더는 원 생성과 **같은 A/B 노브**로 돌린다 — run_log.provenance 에서 복원(§설계수정-2 — 컷 재현).
- 엔진 경로(ai-video·brain)는 형제 디렉토리 추론 + 환경변수 오버라이드 — 로컬·워커 공용.
- 원본 쇼츠는 --no-subtitles 정책이지만 일본어판은 대사 자막을 켠다(§설계수정-3).
  오자막 완화: L2 텔롭 원문을 L1 에 함께 넣어 ASR 오류를 교정한 뒤 번역한다.

실행:
    python scripts/localize_run.py --job-dir <ai-video job dir> [--skip-extract] [--skip-render]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def engine_path(env_key: str, sibling: str) -> Path:
    """형제 엔진 디렉토리 해석 — 환경변수 우선, 없으면 이 레포의 형제. 순수(테스트 대상).

    로컬 운영(`~/ves/<engine>`)과 워커 노드(`$VES_HOME/engines/<engine>`, ves-orchestrator
    config.engine_dir)가 **둘 다 형제 배치**라 같은 규칙 하나로 맞는다. 경로를 절대값으로
    박으면 워커에서 brain .env·ai-video venv·폰트를 전부 못 찾는다."""
    v = os.environ.get(env_key)
    return Path(v) if v else PROJECT.parent / sibling


AI_VIDEO = engine_path("AI_VIDEO_ROOT", "ai-video")
BRAIN = engine_path("BRAIN_ROOT", "ai-improvement-edit-video")
GEN_PY = Path(os.environ.get("AI_VIDEO_GEN_PY") or AI_VIDEO / ".venv" / "bin" / "python")
FONTS_DIR = AI_VIDEO / "app" / "assets" / "fonts"
LOCALES = json.loads((PROJECT / "config" / "locales.json").read_text(encoding="utf-8"))
STATE_PATH = PROJECT / "results" / "localize_state.json"

MODEL_PRO = "gemini-3.1-pro-preview"   # ai-video CLAUDE.md 모델 규칙과 동일 고정

# response_schema — free-form JSON 은 텍스트 안 따옴표에서 곧잘 깨진다(파일럿 실측).
# 구조화 출력을 강제하면 모델 쪽에서 유효 JSON 을 보장한다.
SCHEMA_EXTRACT = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "start_sec": {"type": "number"},
            "end_sec": {"type": "number"},
            "text_ko": {"type": "string"},
            "position": {"type": "string", "enum": ["top", "middle", "bottom"]},
            "kind": {"type": "string",
                     "enum": ["broadcast_telop", "our_subtitle", "our_tts", "top_title", "other"]},
        },
        "required": ["start_sec", "end_sec", "text_ko", "position", "kind"],
    },
}

SCHEMA_TRANSLATE = {
    "type": "object",
    "properties": {
        "segments": {"type": "array", "items": {"type": "object", "properties": {
            "index": {"type": "integer"}, "ja": {"type": "string"}},
            "required": ["index", "ja"]}},
        "tts_cues": {"type": "array", "items": {"type": "object", "properties": {
            "index": {"type": "integer"}, "ja": {"type": "string"}},
            "required": ["index", "ja"]}},
        "top_title_ja": {"type": "string"},
        "youtube_title_ja": {"type": "string"},
        "description_ja": {"type": "string"},
        "hashtags_extra": {"type": "array", "items": {"type": "string"}},
        "telops": {"type": "array", "items": {"type": "object", "properties": {
            "index": {"type": "integer"}, "use": {"type": "boolean"}, "ja": {"type": "string"}},
            "required": ["index", "use"]}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["segments", "tts_cues", "top_title_ja", "youtube_title_ja",
                 "description_ja", "telops"],
}

BACKUP_FILES = [
    "subtitle_segments.json", "edit_plan.json", "checkpoint_story.json",
    "checkpoint_resources.json", "title.txt", "work_title.txt",
]


# ─────────────────────────── 공통 ───────────────────────────

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"runs": {}}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def mark(state, job_id, stage, **extra):
    rec = state["runs"].setdefault(job_id, {})
    rec[stage] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **extra}
    save_state(state)


def gemini_client():
    """GEMINI_API_KEY 는 **환경변수 우선**(워커 /etc/ves/node.env), 없을 때만 brain .env 폴백.
    워커 노드의 brain 체크아웃에는 .env 가 없다(시크릿은 git 밖) — 폴백만 있으면 즉사한다."""
    if not os.environ.get("GEMINI_API_KEY") and (BRAIN / ".env").exists():
        sys.path.insert(0, str(BRAIN / "scripts"))
        from envload import load_env
        load_env(str(BRAIN / ".env"))
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY 없음 — 워커는 /etc/ves/node.env, 로컬은 brain .env 확인")
    from google import genai
    return genai.Client(api_key=key)


def work_locale_cfg(work_title: str, locale: str) -> dict:
    cfg = LOCALES["works"].get(work_title, {}).get(locale)
    if not cfg:
        raise SystemExit(f"locales.json 에 작품 '{work_title}' 의 '{locale}' 항목이 없다")
    return cfg


# ─────────────────────────── L0 백업 ───────────────────────────

def l0_backup(job: Path) -> Path:
    """한국어 원본을 localize_backup_ko/ 에 보존하고 그 경로를 돌려준다. 멱등."""
    backup = job / "localize_backup_ko"
    if not backup.exists():
        backup.mkdir()
        for name in BACKUP_FILES:
            src = job / name
            if src.exists():
                shutil.copy2(src, backup / name)
        print(f"[L0] 백업 생성: {backup}")
    else:
        print(f"[L0] 기존 백업 사용: {backup}")
    ko_video = job / "shorts_ko.mp4"
    if not ko_video.exists():
        src = job / "shorts.mp4"
        if not src.exists():
            raise SystemExit(f"shorts.mp4 가 없다: {job}")
        shutil.copy2(src, ko_video)
        print("[L0] 한국어판 보존: shorts_ko.mp4")
    return backup


# ─────────────────────────── L2 텔롭 추출 ───────────────────────────

EXTRACT_PROMPT = """이 영상은 한국 예능 프로그램의 쇼츠(세로 1080x1920)입니다.
화면에 **렌더링된 텍스트(글자)** 를 전부 찾아 주세요. 음성이 아니라 화면에 보이는 글자입니다.

분류(kind):
- "broadcast_telop": 원본 방송이 넣은 자막·텔롭·효과 문구 (내용 이해에 중요)
- "our_subtitle": 우리가 넣은 대사 자막 (존재만 기록)
- "our_tts": 우리가 넣은 하늘색 내레이션 자막 (존재만 기록)
- "top_title": 최상단 검정 배경 위 흰/노랑 2줄 제목 (존재만 기록)
- "other": 배경 간판·소품·로고 등

각 항목: start_sec, end_sec (숫자), text_ko (원문 그대로), position ("top"/"middle"/"bottom"), kind.
번역은 하지 마세요. JSON 배열만 출력하세요."""


def l2_extract(job: Path, out_dir: Path, client) -> list:
    out_path = out_dir / "onscreen.json"
    if out_path.exists():
        print("[L2] 기존 추출 결과 사용")
        return json.loads(out_path.read_text(encoding="utf-8"))
    from google.genai import types
    video = job / "shorts_ko.mp4"
    print(f"[L2] 업로드: {video.name} ({video.stat().st_size/1e6:.0f}MB)")
    f = client.files.upload(file=str(video))
    while f.state.name == "PROCESSING":
        time.sleep(5)
        f = client.files.get(name=f.name)
    if f.state.name != "ACTIVE":
        raise RuntimeError(f"업로드 실패: state={f.state.name}")
    t0 = time.time()
    resp = client.models.generate_content(
        model=MODEL_PRO, contents=[f, EXTRACT_PROMPT],
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=SCHEMA_EXTRACT),
    )
    data = json.loads(resp.text)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    telops = [d for d in data if d.get("kind") == "broadcast_telop"]
    print(f"[L2] {time.time()-t0:.0f}s — 전체 {len(data)}건, 방송 텔롭 {len(telops)}건")
    return data


# ─────────────────────────── L2b 텔롭 타이밍 재보정 ───────────────────────────
# Gemini 영상 타임스탬프는 런마다 10~20초씩 어긋날 수 있다(파일럿 _74 실측 — 스파이크에선
# 정확했으나 재현 안 됨). 텍스트 목록은 L2(영상 패스)를 믿고, **타이밍은 프레임 샘플링 대조**로
# 다시 잡는다: 1.5초 간격 저해상 프레임을 한 번에 보내 각 프레임에 보이는 텔롭을 매칭.

REFINE_STEP_SEC = 1.5

SCHEMA_REFINE = {
    "type": "object",
    "properties": {"visible": {"type": "array", "items": {"type": "integer"}}},
    "required": ["visible"],
}

MODEL_FLASH = "gemini-3-flash-preview"


def l2b_refine_timing(job: Path, telop_data: list, out_dir: Path, client) -> list:
    """broadcast_telop 의 start/end 를 프레임 대조로 재보정한 목록을 돌려준다."""
    out_path = out_dir / "onscreen_refined.json"
    if out_path.exists():
        print("[L2b] 기존 재보정 결과 사용")
        return json.loads(out_path.read_text(encoding="utf-8"))
    from google.genai import types
    telops = [t for t in telop_data if t.get("kind") == "broadcast_telop"]
    if not telops:
        out_path.write_text("[]", encoding="utf-8")
        return []
    video = job / "shorts_ko.mp4"
    dur = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(video)], capture_output=True, text=True).stdout.strip())
    frames_dir = out_dir / "refine_frames"
    frames_dir.mkdir(exist_ok=True)
    ts = [round(i * REFINE_STEP_SEC, 2) for i in range(int(dur / REFINE_STEP_SEC) + 1)]
    listing = "\n".join(f"{i}: {t['text_ko']}" for i, t in enumerate(telops))
    base_prompt = (
        "이 이미지는 한국 예능 쇼츠의 한 프레임입니다. 아래 텔롭 목록 중 **이 프레임 화면에 "
        "글자로 보이는 것**의 번호만 고르세요. 부분적으로 보여도(잘림·페이드) 포함합니다. "
        "비슷한 다른 문구와 혼동하지 마세요. 하나도 없으면 빈 배열.\n\n"
        f"텔롭 목록:\n{listing}")
    frame_paths = []
    for i, t in enumerate(ts):
        fp = frames_dir / f"f{i:03d}.jpg"
        if not fp.exists():
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", str(video),
                            "-frames:v", "1", "-vf", "scale=360:-1", str(fp)], check=True)
        frame_paths.append(fp)

    t0 = time.time()

    def check_frame(i):
        for attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model=MODEL_FLASH,
                    contents=[base_prompt,
                              types.Part.from_bytes(data=frame_paths[i].read_bytes(),
                                                    mime_type="image/jpeg")],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json", response_schema=SCHEMA_REFINE),
                )
                return i, [int(v) for v in json.loads(resp.text).get("visible", [])]
            except Exception as e:
                if attempt == 2:
                    print(f"[L2b] ⚠️ frame {i} 판독 실패: {e}")
                    return i, []
                time.sleep(2 * (attempt + 1))

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(check_frame, range(len(ts))))
    seen = {i: vis for i, vis in results}
    _ = seen  # (아래 로직이 사용)
    refined = []
    for ti, t in enumerate(telops):
        hits = sorted(fi for fi, vis in seen.items() if ti in vis and 0 <= fi < len(ts))
        if not hits:
            print(f"[L2b] ⚠️ 텔롭 {ti} ({t['text_ko'][:20]!r}) — 프레임 대조 실패, 제외")
            continue
        # 연속 구간으로 묶기 (한 텔롭이 두 번 뜨는 경우 대비)
        groups, cur = [], [hits[0]]
        for fi in hits[1:]:
            if fi - cur[-1] <= 2:
                cur.append(fi)
            else:
                groups.append(cur); cur = [fi]
        groups.append(cur)
        main = max(groups, key=len)          # 병기는 가장 길게 보인 구간 하나만
        start = max(0.0, ts[main[0]] - REFINE_STEP_SEC / 2)
        end = min(dur, ts[main[-1]] + REFINE_STEP_SEC / 2)
        refined.append({**t, "orig_index": ti, "start_sec": round(start, 2), "end_sec": round(end, 2)})
    out_path.write_text(json.dumps(refined, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[L2b] {time.time()-t0:.0f}s — 프레임 {len(ts)}장 대조, 텔롭 {len(refined)}/{len(telops)}건 타이밍 확정")
    return refined


# ─────────────────────────── L1 교정-번역 ───────────────────────────

TRANSLATE_PROMPT = """당신은 한국 예능 쇼츠를 일본 시청자용으로 현지화하는 전문 번역가입니다.
아래 입력(JSON)에는 한 쇼츠의 ① 음성인식 대사 자막(segments) ② 내레이션(tts_cues)
③ 상단 제목(top_title) ④ 화면에 보이는 방송 텔롭 원문(telops) ⑤ 작품 정보가 들어 있습니다.

작업:
1. **ASR 교정**: segments 는 음성인식 결과라 오류가 있습니다. telops(방송 자막 원문)와 문맥을
   근거로 잘못 들린 단어를 교정한 뒤 번역하세요. 교정한 것은 notes 에 한 줄씩 기록.
   비정상적으로 긴 구간(10초↑)에 짧은 텍스트가 걸린 segment 는 환각일 수 있으니 문맥상
   자연스러운 위치의 대사로만 번역하고 notes 에 표시.
2. **대사 번역(segments)**: 일본 쇼츠 자막 문체 — 짧은 구어체, 멤버 관계에 맞는 반말/존댓말,
   감탄사는 일본식으로 의역. 직역 금지. **1:1 정렬 유지(병합·삭제 금지)**.
   줄바꿈을 위해 **구절 경계마다 반각 공백**을 넣으세요(한 구절 최대 14자 목표).
3. **내레이션 번역(tts_cues)**: 짧고 힘있는 예능 내레이션 문체.
4. **상단 제목(top_title_ja)**: 2줄, **각 줄 전각 11자 이내(필수 — 넘으면 화면에서 잘린다)**. 낚시 금지 — 실제 내용 기반으로 궁금증 유발.
5. **유튜브 제목(youtube_title_ja)**: 90자 이내, 반드시 작품명 「{work_display}」 포함,
   일본 쇼츠 어법(w, 〜, ！ 활용 가능).
6. **설명란(description_ja)**: 1~2문장, 해시태그 없이.
7. **텔롭 번역(telops)**: kind 가 broadcast_telop 인 것만. 대사 자막(segments)이나 내레이션과
   내용이 중복되면 use=false. 나머지는 일본 예능 텔롭 문체로 짧게 번역(use=true).
8. **용어집 준수(필수)**: {glossary}

출력은 아래 JSON 스키마만:
{{
  "segments": [{{"index": 0, "ja": "..."}}, ...],
  "tts_cues": [{{"index": 0, "ja": "..."}}, ...],
  "top_title_ja": "1줄목\\n2줄목",
  "youtube_title_ja": "...",
  "description_ja": "...",
  "hashtags_extra": ["#..."],
  "telops": [{{"index": 0, "use": true, "ja": "..."}}, ...],
  "notes": ["교정/특이사항 ..."]
}}"""


TITLE_LINE_MAX = 11   # 전각 기준. 렌더 자동 축소는 13자부터라 CJK 는 그 전에 잘린다(파일럿 실측)


def _fit_title(data: dict, client) -> bool:
    """top_title_ja 각 줄이 상한을 넘으면 그 줄만 축약해 고쳐 쓴다. 고쳤으면 True."""
    lines = data["top_title_ja"].split("\n")
    if all(len(l) <= TITLE_LINE_MAX + 1 for l in lines):
        return False
    from google.genai import types
    resp = client.models.generate_content(
        model=MODEL_FLASH,
        contents=[f"다음 일본어 쇼츠 제목의 각 줄을 전각 {TITLE_LINE_MAX}자 이내로 줄여 주세요. "
                  f"의미·임팩트 유지, 2줄 유지, 줄바꿈은 \\n.\n\n{data['top_title_ja']}"],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema={"type": "object",
                             "properties": {"top_title_ja": {"type": "string"}},
                             "required": ["top_title_ja"]}),
    )
    fixed = json.loads(resp.text)["top_title_ja"]
    print(f"[L1] 제목 축약: {data['top_title_ja']!r} → {fixed!r}")
    data["top_title_ja"] = fixed
    return True


def l1_translate(backup: Path, telop_data: list, work_title: str, wcfg: dict,
                 out_dir: Path, client) -> dict:
    out_path = out_dir / "translation.json"
    if out_path.exists():
        print("[L1] 기존 번역 결과 사용")
        data = json.loads(out_path.read_text(encoding="utf-8"))
        if _fit_title(data, client):
            out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data
    from google.genai import types
    segments = json.loads((backup / "subtitle_segments.json").read_text(encoding="utf-8"))
    resources = json.loads((backup / "checkpoint_resources.json").read_text(encoding="utf-8"))
    cues = [c["cue"]["text"] for c in resources.get("tts_cue_files", [])]
    top_title = (backup / "title.txt").read_text(encoding="utf-8").strip("﻿\n")
    # ⚠️ 인덱스는 broadcast_telop 만 추린 목록의 순서다 — L2b(orig_index)·L3(ass 매칭)와 같은 규약.
    telops = [{"index": i, "start_sec": t["start_sec"], "end_sec": t["end_sec"],
               "text_ko": t["text_ko"], "position": t.get("position")}
              for i, t in enumerate(
                  [t for t in telop_data if t.get("kind") == "broadcast_telop"])]
    payload = {
        "work": {"title_ko": work_title, "display_ja": wcfg["display"], "context": wcfg["context"]},
        "top_title": top_title,
        "segments": [{"index": i, "start_sec": s["start_sec"], "end_sec": s["end_sec"],
                      "ko": s["text"]} for i, s in enumerate(segments)],
        "tts_cues": [{"index": i, "ko": t} for i, t in enumerate(cues)],
        "telops": telops,
    }
    prompt = TRANSLATE_PROMPT.format(work_display=wcfg["display"],
                                     glossary=json.dumps(wcfg["glossary"], ensure_ascii=False))
    t0 = time.time()
    resp = client.models.generate_content(
        model=MODEL_PRO,
        contents=[prompt, json.dumps(payload, ensure_ascii=False, indent=1)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=SCHEMA_TRANSLATE),
    )
    data = json.loads(resp.text)
    # 정렬 검증 — 1:1 이 깨지면 자막 싱크가 깨지므로 즉시 실패
    if len(data["segments"]) != len(segments):
        raise RuntimeError(f"segments 정렬 불일치: ko {len(segments)} vs ja {len(data['segments'])}")
    if len(data["tts_cues"]) != len(cues):
        raise RuntimeError(f"tts_cues 정렬 불일치: ko {len(cues)} vs ja {len(data['tts_cues'])}")
    for g_ko, g_ja in [("혜미리예채파", wcfg["display"])]:
        if g_ja not in data["youtube_title_ja"]:
            raise RuntimeError(f"유튜브 제목에 작품명({g_ja}) 누락: {data['youtube_title_ja']!r}")
    _fit_title(data, client)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[L1] {time.time()-t0:.0f}s — segments {len(data['segments'])} · telop 사용 "
          f"{sum(1 for t in data['telops'] if t.get('use'))}건 · notes {len(data.get('notes', []))}건")
    for n in data.get("notes", []):
        print(f"     note: {n}")
    return data


# ─────────────────────────── L3 적용 ───────────────────────────

def _ass_escape(text: str) -> str:
    return text.replace("{", "(").replace("}", ")").replace("\n", "\\N")


def _fmt_ts(sec: float) -> str:
    h = int(sec // 3600); m = int(sec % 3600 // 60); s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_telop_ass(telop_data: list, translation: dict, font: str, out_path: Path) -> int:
    """방송 텔롭의 일본어 병기 트랙. 대사(430)·TTS(580)와 겹치지 않게 MarginV 720 고정,
    반투명 박스(BorderStyle=3)로 원본 텔롭과 시각적으로 구분한다."""
    telops = [t for t in telop_data if t.get("kind") == "broadcast_telop"] \
        if telop_data and "orig_index" not in telop_data[0] else telop_data
    by_index = {t["index"]: t for t in translation.get("telops", [])}
    lines = []
    for i, t in enumerate(telops):
        tr = by_index.get(t.get("orig_index", i))
        if not tr or not tr.get("use") or not tr.get("ja"):
            continue
        lines.append(f"Dialogue: 0,{_fmt_ts(float(t['start_sec']))},{_fmt_ts(float(t['end_sec']))},"
                     f"Telop,,0,0,0,, {_ass_escape(tr['ja'])}")
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Telop,{font},52,&H00FFFFFF,&H00000000,&H78000000,-1,0,3,5,0,2,70,70,720,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    out_path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def l3_apply(job: Path, backup: Path, translation: dict, telop_data: list,
             wcfg: dict, locale_cfg: dict, out_dir: Path):
    # 대사 자막 — 항상 KO 백업 기준으로 교체(멱등)
    segments = json.loads((backup / "subtitle_segments.json").read_text(encoding="utf-8"))
    for seg, tr in zip(segments, translation["segments"]):
        seg["text"] = tr["ja"]
        # ASR 환각성 초장 구간 방어 — 짧은 대사가 10초 넘게 떠 있으면 어색하다(파일럿 _74 실측 22s).
        span = float(seg["end_sec"]) - float(seg["start_sec"])
        if span > 8.0 and len(tr["ja"]) <= 20:
            seg["end_sec"] = float(seg["start_sec"]) + 4.0
    (job / "subtitle_segments.json").write_text(
        json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")

    # 상단 제목 — 렌더 정본(checkpoint_story) + 발행용(edit_plan) 둘 다 (SPIKE §설계수정-1)
    story = json.loads((backup / "checkpoint_story.json").read_text(encoding="utf-8"))
    if "variants" in story:
        for v in story["variants"]:
            v["title_text"] = translation["top_title_ja"]
    else:
        story["title_text"] = translation["top_title_ja"]
    (job / "checkpoint_story.json").write_text(
        json.dumps(story, ensure_ascii=False, indent=2), encoding="utf-8")

    plan = json.loads((backup / "edit_plan.json").read_text(encoding="utf-8"))
    plan["layout"]["top_title"] = translation["top_title_ja"]
    plan["layout"]["bottom_label"] = wcfg["display"]
    (job / "edit_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    # TTS cue 텍스트 (오디오는 손대지 않는다 — 자막만 일본어)
    resources = json.loads((backup / "checkpoint_resources.json").read_text(encoding="utf-8"))
    for c, tr in zip(resources.get("tts_cue_files", []), translation["tts_cues"]):
        c["cue"]["text"] = tr["ja"]
    (job / "checkpoint_resources.json").write_text(
        json.dumps(resources, ensure_ascii=False, indent=2), encoding="utf-8")

    n = build_telop_ass(telop_data, translation, locale_cfg["telop_font"], out_dir / "telops.ass")
    print(f"[L3] 적용 완료 — 대사 {len(segments)}건 · 텔롭 병기 {n}건 (telops.ass)")


# ─────────────────────────── L3t TTS 일본어 재합성 ───────────────────────────
# 사용자 지시(2026-08-04): TTS 내레이션 오디오도 일본어로. 원본 대사 음성만 원어(한국어) 유지.
# cue 텍스트는 L3 가 이미 일본어로 바꿔 놨으므로, 같은 텍스트로 mp3 를 다시 합성해
# 같은 경로에 덮어쓴다(렌더가 그 경로의 mp3 를 읽어 오디오 믹스 + 자막 길이 산정).
# 길이는 cue 계획 창(start~end) 안에 들어올 때까지 rate 를 올려 재합성한다.

def _audio_dur(p: Path) -> float:
    try:
        return float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(p)], capture_output=True, text=True).stdout.strip())
    except ValueError:
        return 0.0


def l3t_tts(job: Path, backup: Path, locale_cfg: dict):
    res_path = job / "checkpoint_resources.json"
    resources = json.loads(res_path.read_text(encoding="utf-8"))
    cues = resources.get("tts_cue_files", [])
    if not cues:
        print("[L3t] TTS cue 없음 — 생략")
        return
    import asyncio
    import edge_tts
    vmap = locale_cfg.get("tts_voice_map", {})
    speed_base = {"very_slow": -25, "slow": -10, "normal": 0, "fast": 10, "very_fast": 25}
    for c in cues:
        cue = c["cue"]
        mp3 = Path(c["path"])
        bk = backup / mp3.name
        if mp3.exists() and not bk.exists():
            shutil.copy2(mp3, bk)                      # 한국어 mp3 보존(멱등)
        prof = vmap.get(cue.get("voice")) or vmap.get("_default")
        if prof is None:                               # chat_* 등 multilingual — 원 보이스 유지
            print(f"[L3t] cue {c['cue_index']}: voice {cue.get('voice')!r} 매핑 없음 — 원 보이스로 일본어 합성 불가, 건너뜀")
            continue
        base = speed_base.get(cue.get("speed", "normal"), 0)
        window = float(cue["end_sec"]) - float(cue["start_sec"])
        text = cue["text"]
        dur = 0.0
        for bump in (0, 15, 30):
            r = base + bump
            rate = f"{'+' if r >= 0 else ''}{r}%"

            async def _run():
                await edge_tts.Communicate(
                    text, prof["voice_id"], rate=rate, pitch=prof.get("pitch", "+0Hz")
                ).save(str(mp3))

            asyncio.run(_run())
            dur = _audio_dur(mp3)
            if 0.0 < dur <= window * 0.95:
                break
        if dur > window:
            print(f"[L3t] ⚠️ cue {c['cue_index']}: {dur:.1f}s > 창 {window:.1f}s — 검수 필요")
        cue["fit_actual_sec"] = round(dur, 3)
        cue["voice_ja"] = prof["voice_id"]
        print(f"[L3t] cue {c['cue_index']}: {text!r} → {prof['voice_id']} {dur:.1f}s (창 {window:.1f}s)")
    res_path.write_text(json.dumps(resources, ensure_ascii=False, indent=2), encoding="utf-8")


# ─────────────────────────── L4 재렌더 + 텔롭 번인 ───────────────────────────

def render_flags(run_log: dict) -> list:
    """재렌더 A/B 노브 복원. 순수(테스트 대상).

    컷을 프레임 단위로 재현하려면 원 생성과 **같은 노브**여야 한다(SPIKE §설계수정-2: 어긋나면
    49.7s→53.3s 로 컷이 달라져 자막 싱크가 통째로 깨진다). 정본은 그 런의
    run_log.provenance.config.app — **실제로 쓰인 값**이다. brain loop_policy.gen_flags_base 는
    '현재 정책'이라 런 이후 바뀌었을 수 있고, ves-orchestrator 경로에서는 work_order.knob_config
    가 정책을 덮으므로 애초에 일치를 보장하지 못한다. provenance 가 없는 옛 런만 정책으로 폴백."""
    app = ((run_log or {}).get("provenance") or {}).get("config", {}).get("app") or {}
    flags = []
    prof = app.get("silence_cut_profile")
    if prof in ("aggressive", "conservative"):
        flags += ["--silence-profile", prof]
    # length=tight 의 지문 — cli._apply_ab_env 가 세팅하는 세 값 그대로(45/50/1.1)
    try:
        tight = (int(app.get("target_duration_sec")) == 45
                 and int(app.get("max_duration_sec")) == 50
                 and abs(float(app.get("max_duration_tolerance")) - 1.1) < 1e-6)
    except (TypeError, ValueError):
        tight = False
    if tight:
        flags += ["--length-profile", "tight"]
    if not flags:
        try:
            flags = list(json.loads((BRAIN / "config" / "loop_policy.json")
                                    .read_text(encoding="utf-8"))["gen_flags_base"])
        except (OSError, ValueError, KeyError):
            flags = []
    if "--loudness-lufs" not in flags:
        flags += ["--loudness-lufs", "-14"]   # 쇼츠 표준. 컷에는 영향 없다(오디오 전용)
    return flags


def _provision_fonts(locale_cfg: dict):
    """일본어 폰트 자동 프로비저닝 — ArialUnicode 는 macOS 시스템 폰트(재배포 라이선스
    문제로 레포에 못 넣는다)라, 없으면 시스템 사본을 ai-video assets 로 복사한다.
    전 워커 노드가 맥이라는 전제(ves-orchestrator MACHINE_SETUP)."""
    sys_font = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
    for key in ("title_font", "subtitle_font", "telop_font"):
        name = locale_cfg.get(key)
        if name != "ArialUnicode":
            continue
        dst = FONTS_DIR / "ArialUnicode.ttf"
        if not dst.exists():
            if not sys_font.exists():
                raise SystemExit(f"일본어 폰트 없음: {dst} — macOS 시스템 폰트({sys_font})도 없다")
            FONTS_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sys_font, dst)
            print(f"[L4] 폰트 프로비저닝: {sys_font.name} → {dst}")
        break


def l4_render(job: Path, wcfg: dict, locale_cfg: dict, out_dir: Path):
    _provision_fonts(locale_cfg)
    run_log = json.loads((job / "run_log.json").read_text(encoding="utf-8"))
    video_path = run_log["input"]["video_path"]
    if not Path(video_path).exists():
        raise SystemExit(f"소스 영상이 없다: {video_path}")
    gen_flags = render_flags(run_log)
    print(f"[L4] 재현 플래그: {' '.join(gen_flags)}")

    cmd = [str(GEN_PY), "-m", "app.cli", "create_shorts",
           "--title", wcfg["display"],
           "--video", video_path,
           "--outdir", str(job.parent),
           "--from-step", "render", "--job-id", job.name,
           "--design-title-font", locale_cfg["title_font"],
           "--design-subtitle-font", locale_cfg["subtitle_font"],
           "--max-shorts", "1", *gen_flags]
    print(f"[L4] 재렌더: {' '.join(cmd[3:])}")
    t0 = time.time()
    r = subprocess.run(cmd, cwd=AI_VIDEO, capture_output=True, text=True, timeout=1800)
    (out_dir / "rerender.log").write_text(r.stdout + "\n--- stderr ---\n" + r.stderr, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"재렌더 실패 rc={r.returncode} — {out_dir/'rerender.log'} 확인")
    rendered = job / "shorts.mp4"
    # 컷 재현 검증 — 원본과 길이가 다르면 자막 싱크가 깨진 것 (SPIKE §설계수정-2)
    def dur(p):
        return float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(p)], capture_output=True, text=True).stdout.strip())
    d_ko, d_ja = dur(job / "shorts_ko.mp4"), dur(rendered)
    if abs(d_ko - d_ja) > 0.05:
        raise RuntimeError(f"컷 길이 불일치: ko {d_ko:.3f}s vs ja {d_ja:.3f}s — gen_flags 재현 실패 의심")
    print(f"[L4] 재렌더 완료 {time.time()-t0:.0f}s (길이 {d_ja:.3f}s = 원본 일치)")

    # 텔롭 병기 번인 (오디오 무손실 copy)
    telop_ass = out_dir / "telops.ass"
    notelop = job / "shorts_ja_notelop.mp4"
    shutil.move(rendered, notelop)
    ass_arg = str(telop_ass).replace(":", "\\:")
    fonts_arg = str(FONTS_DIR).replace(":", "\\:")
    r2 = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(notelop),
         "-vf", f"ass='{ass_arg}':fontsdir='{fonts_arg}'",
         "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-c:a", "copy",
         str(rendered)], capture_output=True, text=True, timeout=600)
    if r2.returncode != 0:
        shutil.move(notelop, rendered)          # 원복
        raise RuntimeError(f"텔롭 번인 실패: {r2.stderr[-500:]}")
    print(f"[L4] 텔롭 번인 완료 → shorts.mp4 (중간본 shorts_ja_notelop.mp4 보존)")


# ─────────────────────────── L5 메타데이터 ───────────────────────────

def build_ko_ja_pairs(backup: Path, out_dir: Path, translation: dict,
                      max_items: int = 40) -> dict:
    """한글⇄일본어 대역(8/14 사용자 요청) — 관제 검수 카드에서 일본어 제목·자막을
    한글과 나란히 본다. 원문은 백업(KO)·L2 텔롭에서, 번역은 translation 에서.
    실패는 조용히 비운다(대역은 검수 편의지 렌더 정본이 아니다). 순수 — 테스트 대상."""
    pairs = {"top_title": None, "subs": [], "telops": []}
    try:
        ep = json.loads((backup / "edit_plan.json").read_text(encoding="utf-8"))
        ko = ((ep.get("layout") or {}).get("top_title") or "").strip()
        pairs["top_title"] = {"ko": ko or None, "ja": translation.get("top_title_ja")}
    except Exception:
        pairs["top_title"] = {"ko": None, "ja": translation.get("top_title_ja")}
    try:
        segs = json.loads((backup / "subtitle_segments.json").read_text(encoding="utf-8"))
        for seg, tr in list(zip(segs, translation.get("segments") or []))[:max_items]:
            pairs["subs"].append({"start": seg.get("start_sec"),
                                  "ko": seg.get("text"), "ja": tr.get("ja")})
    except Exception:
        pass
    try:
        telops = json.loads((out_dir / "onscreen.json").read_text(encoding="utf-8"))
        by_idx = {t.get("index"): t for t in (translation.get("telops") or [])}
        for i, t in enumerate(telops[:max_items]):
            tr = by_idx.get(i) or {}
            if tr.get("use") is False:
                continue
            pairs["telops"].append({"ko": t.get("text_ko"), "ja": tr.get("ja")})
    except Exception:
        pass
    return pairs


def l5_metadata(job: Path, translation: dict, wcfg: dict, out_dir: Path):
    hashtags = list(dict.fromkeys(wcfg.get("hashtags_base", []) + translation.get("hashtags_extra", [])))
    desc_lines = [translation["description_ja"], ""]
    desc_lines += wcfg.get("notice_lines", [])
    desc_lines += ["", " ".join(hashtags)]
    meta = {
        "youtube_title": translation["youtube_title_ja"],
        "description": "\n".join(desc_lines),
        "tags": [h.lstrip("#") for h in hashtags],
        "top_title_burned": translation["top_title_ja"],
        "notes": translation.get("notes", []),
        # 한글 대역(8/14) — 검수 카드가 그대로 내려받아 보여준다
        "ko_ja_pairs": build_ko_ja_pairs(job / "localize_backup_ko", out_dir, translation),
        "_publish": "publish_youtube.py --title 로 제목 오버라이드. 설명란 필수 고지(권리)는 "
                    "publish_youtube 가 laeebly 기준으로 별도 추가하므로 여기 description 은 참고용 본문.",
    }
    (out_dir / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[L5] metadata.json — 제목: {meta['youtube_title']}")


# ─────────────────────────── main ───────────────────────────

def main():
    ap = argparse.ArgumentParser(description="ai-video job 디렉토리 일본어 현지화")
    ap.add_argument("--job-dir", required=True)
    ap.add_argument("--locale", default="ja")
    ap.add_argument("--skip-render", action="store_true", help="L4 재렌더 생략(번역까지만)")
    args = ap.parse_args()

    job = Path(args.job_dir).resolve()
    if not job.is_dir():
        raise SystemExit(f"job 디렉토리가 없다: {job}")
    work_title = (job / "localize_backup_ko" / "work_title.txt")
    wt_src = work_title if work_title.exists() else job / "work_title.txt"
    work = wt_src.read_text(encoding="utf-8").strip("﻿\n ")
    wcfg = work_locale_cfg(work, args.locale)
    locale_cfg = LOCALES["locales"][args.locale]
    out_dir = job / f"localize_{args.locale}"
    out_dir.mkdir(exist_ok=True)

    state = load_state()
    print(f"=== 현지화 시작: {job.name} ({work} → {args.locale}) ===")
    backup = l0_backup(job)
    mark(state, job.name, "L0")

    client = gemini_client()
    telop_data = l2_extract(job, out_dir, client)
    mark(state, job.name, "L2")
    telop_refined = l2b_refine_timing(job, telop_data, out_dir, client)
    mark(state, job.name, "L2b")
    translation = l1_translate(backup, telop_data, work, wcfg, out_dir, client)
    mark(state, job.name, "L1")
    l3_apply(job, backup, translation, telop_refined, wcfg, locale_cfg, out_dir)
    mark(state, job.name, "L3")
    l3t_tts(job, backup, locale_cfg)
    mark(state, job.name, "L3t")
    if not args.skip_render:
        l4_render(job, wcfg, locale_cfg, out_dir)
        mark(state, job.name, "L4")
    l5_metadata(job, translation, wcfg, out_dir)
    mark(state, job.name, "L5", done=True)
    print(f"=== 완료: {job / 'shorts.mp4'} (일본어판) · 검수자료 {out_dir} ===")


if __name__ == "__main__":
    main()
