"""선별 더빙 (C-9) — Level C 한정. 일본어 자막 → TTS 초안.

ja 자막(ja.srt/ja.ass)을 입력으로 ElevenLabs 등 TTS 호출, persona 보이스 디렉션 반영.
한국 성우 클로닝이 아니라 "일본 캐릭터 보이스 디렉션" 기본.
출력: outputs/{video_id}/dub_ja_draft.wav + alignment_report.json.
⚠ retention 리스크·hero 영상은 사람/성우 검토. 자동 영상 합성·게시 금지(드래프트 오디오까지만).

[Level C 가드] level != C 면 거부. 자막 파싱·정렬 리포트는 순수, TTS/ffmpeg 만 외부.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pathlib import Path  # noqa: E402
from typing import Any, Optional  # noqa: E402

from engine import common  # noqa: E402
from engine.common import ensure_dir, get_logger, get_secret, load_config, resolve_path, write_json  # noqa: E402

log = get_logger("dub")


# ── 순수 헬퍼 ─────────────────────────────────────────────────────────────
def require_level_c(level: str) -> None:
    if level != "C":
        raise ValueError(f"더빙은 Level C 한정. 현재 level={level}. 게이트/등급 확인.")


def _srt_time(t: str) -> float:
    h, m, rest = t.split(":")
    s, _, ms = rest.replace(".", ",").partition(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + (int(ms) / 1000 if ms else 0.0)


def _ass_time(t: str) -> float:
    h, m, rest = t.split(":")
    s, _, cs = rest.partition(".")
    return int(h) * 3600 + int(m) * 60 + int(s) + (int(cs) / 100 if cs else 0.0)


def parse_segments(subtitle_path: str) -> list[dict[str, Any]]:
    """ja.srt 또는 ja.ass → [{start,end,text}] (초)."""
    text = Path(subtitle_path).read_text(encoding="utf-8")
    segs: list[dict[str, Any]] = []
    if subtitle_path.endswith(".ass"):
        for line in text.splitlines():
            if not line.startswith("Dialogue:"):
                continue
            fields = line[len("Dialogue:"):].split(",", 9)
            if len(fields) < 10:
                continue
            body = re.sub(r"\{[^}]*\}", "", fields[9]).replace("\\N", " ").strip()
            if body:
                segs.append({"start": _ass_time(fields[1].strip()),
                             "end": _ass_time(fields[2].strip()), "text": body})
    else:  # SRT
        for block in re.split(r"\n\s*\n", text.strip()):
            lines = [ln for ln in block.splitlines() if ln.strip()]
            if len(lines) < 2:
                continue
            tl = next((ln for ln in lines if "-->" in ln), None)
            if not tl:
                continue
            start, _, end = tl.partition("-->")
            body = " ".join(lines[lines.index(tl) + 1:]).strip()
            if body:
                segs.append({"start": _srt_time(start.strip()),
                             "end": _srt_time(end.strip()), "text": body})
    segs.sort(key=lambda s: s["start"])
    return segs


def atempo_filters(speed: float) -> str:
    """ffmpeg atempo 는 0.5~2.0 만 지원 → 필요한 배속을 체인으로 분해한 필터 문자열."""
    speed = max(0.25, min(4.0, speed))
    parts: list[str] = []
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    parts.append(f"atempo={speed:.4f}")
    return ",".join(parts)


# ── 더빙 견고화 순수 로직 (싱크 / 환각방지 / 클리핑) ──────────────────────
def _fit_speed(dur: float, target: float, max_speedup: float = 1.6,
               min_slowdown: float = 0.7) -> float:
    """슬롯에 맞추기 위한 배속 비율. 과속(max_speedup)·과늘림(min_slowdown) 클램프."""
    if dur <= 0 or target <= 0:
        return 1.0
    speed = dur / target
    return min(speed, max_speedup) if speed > 1 else max(speed, min_slowdown)


def _needs_truncate(dur: float, max_len: Optional[float]) -> bool:
    """배속 후에도 슬롯(다음 세그 시작)을 넘으면 잘라야 한다(드론/겹침 방지)."""
    return max_len is not None and dur > max_len + 0.05


def segment_hard_caps(spans: list[tuple[float, float]], guard: float = 0.05,
                      tail: float = 0.5) -> list[float]:
    """각 세그가 '다음 세그 시작'을 침범하지 않도록 최대 길이 캡 산출.

    한 세그의 합성이 환각으로 길어져도 다음 발화 위로 겹쳐 깔리는('에~~~' 드론)
    현상을 구조적으로 차단한다. 마지막 세그는 슬롯+tail 까지 허용.
    """
    caps: list[float] = []
    n = len(spans)
    for i, (s, e) in enumerate(spans):
        if i + 1 < n:
            caps.append(max(0.2, spans[i + 1][0] - s - guard))
        else:
            caps.append((e - s) + tail)
    return caps


def synthesize_with_retry(synth_fn, max_dur: float, tries: int = 5):
    """TTS 환각(비정상적으로 긴 출력) 방지: max_dur 이하가 나올 때까지 재합성.

    synth_fn() → (sr, audio[len 측정 가능]). 전부 길면 가장 짧은 결과를 반환(이후 캡됨).
    """
    best = None
    best_d = float("inf")
    for _ in range(max(1, tries)):
        sr, audio = synth_fn()
        d = (len(audio) / sr) if sr else 0.0
        if d < best_d:
            best, best_d = (sr, audio), d
        if d <= max_dur:
            return sr, audio
    return best


def f0_median(audio, sr: int, fmin: float = 80.0, fmax: float = 550.0) -> float:
    """프레임 자기상관 기반 중위 F0(Hz). 무성/저에너지 프레임 제외. 0=측정 불가."""
    import numpy as np
    x = np.asarray(audio, dtype=np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    peak = np.abs(x).max()
    if not sr or peak <= 0:
        return 0.0
    x = x / peak
    w, hop = int(0.04 * sr), int(0.01 * sr)
    vals = []
    for i in range(0, len(x) - w, hop):
        f = x[i:i + w]
        if np.sqrt((f ** 2).mean()) < 0.05:
            continue
        f = f - f.mean()
        ac = np.correlate(f, f, "full")[w - 1:]
        lo, hi = int(sr / fmax), int(sr / fmin)
        if hi >= len(ac):
            continue
        pk = int(np.argmax(ac[lo:hi])) + lo
        if ac[pk] > 0.3 * ac[0]:
            vals.append(sr / pk)
    import statistics
    return statistics.median(vals) if vals else 0.0


def pitch_distance_octaves(f0_a: float, f0_b: float) -> float:
    """두 F0 간 거리(옥타브). 측정 불가(0)면 inf — 비교 불가는 최악으로 취급."""
    import math
    if f0_a <= 0 or f0_b <= 0:
        return float("inf")
    return abs(math.log2(f0_a / f0_b))


def synth_level(audio) -> float:
    """합성 오디오의 정규화 피크(0~1). int/float dtype 모두 지원.

    GPT-SoVITS 가 간혹 '사실상 무음' 실패 합성을 내는데(실측: 커몬2 업로드본 —
    무음 후보가 피치 매칭을 통과한 뒤 정규화로 증폭돼 잡음이 됨), 피크 정규화 기반
    F0 측정은 이를 못 거른다 → 절대 레벨로 별도 게이트."""
    import numpy as np
    x = np.asarray(audio)
    if x.size == 0:
        return 0.0
    if np.issubdtype(x.dtype, np.integer):
        return float(np.abs(x).max()) / float(np.iinfo(x.dtype).max)
    return float(np.abs(x).max())


def _norm_scale(peak: float, target: float = 0.9) -> float:
    """보이스 트랙 피크 정규화 배율(헤드룸 확보 → limiter 펌핑 최소화). 무음 보호."""
    return (target / peak) if peak > 0 else 1.0


def _detect_lang(text: str, default: str = "ja") -> str:
    """세그 텍스트 언어 추정(영어 대사는 영어로 합성 유지). 라틴문자 위주면 en."""
    letters = re.sub(r"[^A-Za-z぀-ヿ一-鿿가-힣]", "", text)
    if not letters:
        return default
    ascii_alpha = sum(1 for c in letters if c.isascii() and c.isalpha())
    return "en" if ascii_alpha / len(letters) > 0.7 else default


# 먹방/ASMR 의성어·감탄(원본 유지 대상). 더빙은 '실제 대사'만(dialogue_only).
_ONOMATOPOEIA = {"음", "으음", "흠", "아", "어", "오", "와", "우와", "워", "앙", "냠", "냠냠",
                 "으", "읏", "하", "호", "헉", "캬", "얍", "요", "에", "음냠", "쩝", "후",
                 "휴", "으하", "으아", "쓰", "짱", "컥", "냥", "자", "야"}


def _is_dialogue(text: str) -> bool:
    """실제 대사 여부(true) vs 씹는소리/감탄(false). ASMR 리액션은 원본 유지하려 걸러냄."""
    s = re.sub(r"[\s!?.,~…♪♥★\-]+", "", text)
    if not s:
        return False
    if re.fullmatch(r"[A-Za-z' ]+", text) and len(text) <= 12:   # 짧은 영어 추임새
        return False
    if re.fullmatch(r"[\d,\s]+", text):                          # 숫자 카운트만
        return False
    kor = re.findall(r"[가-힣]", s)
    if len(kor) <= 2:                                            # 한글 2음절 이하 = 감탄
        return False
    if s in _ONOMATOPOEIA or (len(set(kor)) == 1):              # 의성어 / 같은 음절 반복(앙앙앙)
        return False
    return True


def build_alignment_report(video_id: str, segments: list[dict[str, Any]],
                           voice_id: str) -> dict[str, Any]:
    return {
        "_warning": "더빙 초안. retention 리스크·hero 는 사람/성우 검토. 자동 게시 금지.",
        "video_id": video_id,
        "voice_id": voice_id,
        "segment_count": len(segments),
        "total_speech_sec": round(sum(s["end"] - s["start"] for s in segments), 2),
        "segments": segments,
    }


# ── TTS 백엔드 (lazy) ─────────────────────────────────────────────────────
def dub_backend(config: dict[str, Any]) -> str:
    """gptsovits(루피 음색 크로스링구얼 클로닝, 권장) | xtts | elevenlabs."""
    return config.get("dub", {}).get("tts_backend", "elevenlabs")


def segment_ext(config: dict[str, Any]) -> str:
    return ".wav" if dub_backend(config) in ("xtts", "gptsovits") else ".mp3"


_XTTS_MODEL = None  # 프로세스당 1회 로드(무거움)


def _xtts_model(config: dict[str, Any]):
    global _XTTS_MODEL
    if _XTTS_MODEL is None:
        from TTS.api import TTS  # coqui-tts

        name = config.get("dub", {}).get("xtts_model",
                                         "tts_models/multilingual/multi-dataset/xtts_v2")
        _XTTS_MODEL = TTS(name)
    return _XTTS_MODEL


def _synthesize_xtts(text: str, speaker_wav: str, config: dict[str, Any]) -> bytes:
    """XTTS-v2 크로스링구얼 보이스 클로닝: speaker_wav 목소리로 language 음성 합성."""
    import tempfile

    lang = config.get("dub", {}).get("language", "ja")
    out = tempfile.mktemp(suffix=".wav")
    _xtts_model(config).tts_to_file(text=text, speaker_wav=speaker_wav, language=lang, file_path=out)
    return Path(out).read_bytes()


_GSV = None  # GPT-SoVITS 핸들(프로세스당 1회 로드)


def reset_gptsovits_handle() -> None:
    """오염된 추론 상태 초기화 — 퇴화 레퍼런스가 모듈 내부 캐시를 오염시켜 이후
    '모든' 레퍼런스의 합성이 무음이 되는 실측 사례(2026-07-08 커몬2) 대응.
    다음 _gptsovits_handle 호출 시 모듈·가중치 재로드(~20s)."""
    global _GSV
    _GSV = None
    import sys as _sys
    for name in list(_sys.modules):
        if "inference_webui" in name:
            _sys.modules.pop(name, None)


def _gptsovits_handle(config: dict[str, Any]):
    """GPT-SoVITS 추론 모듈 로드 + 가중치 적용(1회). config.dub.gptsovits 로 경로 지정.

    repo_dir/model_dir 는 상대경로면 프로젝트 루트 기준. CPU 로 구동(Apple Silicon 안정).
    """
    global _GSV
    if _GSV is not None:
        return _GSV
    import os as _os
    import sys as _sys

    g = config.get("dub", {}).get("gptsovits", {})
    repo = resolve_path(g.get("repo_dir", "outputs/GPT-SoVITS"))
    _os.environ.setdefault("is_half", "False")
    _sys.path.insert(0, str(repo))
    _sys.path.insert(0, str(repo / "GPT_SoVITS"))
    cwd = _os.getcwd()
    _os.chdir(repo)                                  # 상대 pretrained_models 경로 해석용
    try:
        import GPT_SoVITS.inference_webui as iw
        iw.device = "cpu"; iw.is_half = False
        md = repo / g.get("model_dir", "GPT_SoVITS/pretrained_models/gsv-v2final-pretrained")
        iw.change_gpt_weights(gpt_path=str(md / g.get(
            "gpt_ckpt", "s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt")))
        iw.change_sovits_weights(sovits_path=str(md / g.get("sovits_ckpt", "s2G2333k.pth")))
        from tools.i18n.i18n import I18nAuto
        i18n = I18nAuto()
        _GSV = {"iw": iw, "lang": {"ja": i18n("日文"), "en": i18n("英文"), "ko": i18n("韩文")}}
    finally:
        _os.chdir(cwd)
    return _GSV


def _synthesize_gptsovits(text: str, lang: str, config: dict[str, Any]) -> bytes:
    """루피 음색 크로스링구얼 클로닝(한국어 ref → ja/en 합성). 멀티레퍼런스 + 환각방지 재시도."""
    import tempfile
    from types import SimpleNamespace

    import soundfile as sf

    g = config.get("dub", {}).get("gptsovits", {})
    h = _gptsovits_handle(config)
    iw = h["iw"]
    aux = [SimpleNamespace(name=str(resolve_path(p))) for p in g.get("aux_refs", [])]

    def _one():
        res = iw.get_tts_wav(
            ref_wav_path=str(resolve_path(g["ref_wav"])),
            prompt_text=g["prompt_text"],
            prompt_language=h["lang"][g.get("prompt_lang", "ko")],
            text=text, text_language=h["lang"].get(lang, h["lang"]["ja"]),
            top_k=int(g.get("top_k", 20)), top_p=float(g.get("top_p", 0.6)),
            temperature=float(g.get("temperature", 0.6)), inp_refs=(aux or None))
        sr, audio = list(res)[-1]
        return sr, audio

    max_dur = float(g.get("max_synth_dur", 12.0))
    tries = int(g.get("retry_tries", 6))
    pitch_tries = int(g.get("pitch_match_tries", 3))
    ref_f0 = 0.0
    if pitch_tries > 1:
        try:
            rx, rsr = sf.read(str(resolve_path(g["ref_wav"])))
            ref_f0 = f0_median(rx, rsr)
        except Exception:
            ref_f0 = 0.0
    min_level = float(g.get("min_synth_level", 0.05))
    if ref_f0 > 0:
        # 합성 피치는 회차별 편차가 큼(실측: 원본 405Hz 인데 244/274Hz 회차) →
        # 후보 N개 중 레퍼런스 피치에 가장 가까운 것 선택. 0.15 옥타브(~10%) 안이면 조기 종료.
        # 단 '사실상 무음' 후보(synth_level < min_level)는 피치와 무관하게 기각 —
        # 무음이 선택되면 이후 정규화가 잡음을 증폭한다(2026-07-08 커몬2 실측).
        best, best_dist = None, float("inf")
        for i in range(max(1, pitch_tries)):
            sr, audio = synthesize_with_retry(_one, max_dur=max_dur, tries=tries)
            lvl = synth_level(audio)
            if lvl < min_level:
                log.warning("무음성 합성 후보 기각(level=%.3f < %.2f) — 재시도", lvl, min_level)
                continue
            dist = pitch_distance_octaves(f0_median(audio, sr), ref_f0)
            if dist < best_dist:
                best, best_dist = (sr, audio), dist
            if best_dist <= 0.15:
                break
        if best is None:
            raise RuntimeError(
                f"합성 {pitch_tries}회 전부 무음성(level<{min_level}) — "
                "레퍼런스/모델 상태 확인 필요(쓰레기 게시 방지 위해 실패 처리)")
        sr, audio = best
        log.info("피치 매칭: ref=%.0fHz, 선택 후보 거리=%.2f oct (%d회 시도)",
                 ref_f0, best_dist, i + 1)
    else:
        sr, audio = synthesize_with_retry(_one, max_dur=max_dur, tries=tries)
        if synth_level(audio) < min_level:
            raise RuntimeError(f"무음성 합성(level<{min_level}) — 쓰레기 게시 방지 위해 실패 처리")
    out = tempfile.mktemp(suffix=".wav")
    sf.write(out, audio, sr)
    return Path(out).read_bytes()


def _synthesize_elevenlabs(text: str, voice_id: str, config: dict[str, Any]) -> bytes:
    key = get_secret("ELEVENLABS_API_KEY", required=True)
    try:
        from elevenlabs.client import ElevenLabs
    except ImportError as e:
        raise ImportError("elevenlabs 필요: pip install elevenlabs") from e
    client = ElevenLabs(api_key=key)
    audio = client.text_to_speech.convert(
        voice_id=voice_id, text=text,
        model_id=config.get("dub", {}).get("tts_model", "eleven_multilingual_v2"),
        output_format="mp3_44100_128")
    return b"".join(audio) if hasattr(audio, "__iter__") else audio


def synthesize_segment(text: str, config: dict[str, Any], voice_id: Optional[str] = None,
                       speaker_wav: Optional[str] = None, lang: Optional[str] = None) -> bytes:
    backend = dub_backend(config)
    if backend == "gptsovits":
        seg_lang = lang or _detect_lang(text, config.get("dub", {}).get("language", "ja"))
        return _synthesize_gptsovits(text, seg_lang, config)
    if backend == "xtts":
        if not speaker_wav:
            raise ValueError("xtts 백엔드: speaker_wav(클로닝용 음성 샘플) 필요")
        return _synthesize_xtts(text, speaker_wav, config)
    if not voice_id:
        raise ValueError("elevenlabs 백엔드: voice_id 필요")
    return _synthesize_elevenlabs(text, voice_id, config)


def dub(video_id: str, subtitle_path: str, level: str, config: dict[str, Any],
        voice_id: Optional[str] = None, speaker_wav: Optional[str] = None) -> dict[str, Any]:
    require_level_c(level)
    backend = dub_backend(config)
    if backend == "gptsovits":
        # 레퍼런스(루피) 음성으로 크로스링구얼 클로닝 → voice_id/speaker_wav 불필요.
        gsv = config.get("dub", {}).get("gptsovits", {})
        if not gsv.get("ref_wav"):
            raise ValueError("gptsovits 백엔드: config.dub.gptsovits.ref_wav(레퍼런스 음성) 필요")
        voice_ref = gsv["ref_wav"]
    elif backend == "xtts":
        speaker_wav = speaker_wav or config.get("dub", {}).get("speaker_wav")
        if not speaker_wav:
            raise ValueError("xtts 백엔드: --speaker(클로닝용 루피 음성 샘플) 또는 config.dub.speaker_wav 필요")
        voice_ref = speaker_wav
    else:
        voice_id = voice_id or config.get("dub", {}).get("voice_id", "")
        if not voice_id:
            raise ValueError("elevenlabs 백엔드: voice_id 필요. --voice 또는 config.dub.voice_id")
        voice_ref = voice_id

    segments = parse_segments(subtitle_path)
    base = ensure_dir(resolve_path(f"{config['paths']['outputs_dir']}/{video_id}"))
    seg_dir = ensure_dir(base / "dub_segments")
    ext = segment_ext(config)
    log.warning("Level C 더빙 초안 생성(backend=%s). hero/리텐션 리스크는 사람 검토 필수.", backend)

    fit = config.get("dub", {}).get("fit_to_timing", True)
    max_sp = float(config.get("dub", {}).get("max_speedup", 1.6))
    caps = segment_hard_caps([(s.get("start", 0.0), s.get("end", 0.0)) for s in segments])
    seg_files: list[tuple[float, Path]] = []
    for i, seg in enumerate(segments):
        data = synthesize_segment(seg["text"], config, voice_id=voice_id, speaker_wav=speaker_wav)
        fp = seg_dir / f"seg_{i:04d}{ext}"
        slot = seg.get("end", 0) - seg.get("start", 0)
        if fit and slot > 0:                          # 슬롯 길이에 맞게 time-stretch(싱크) + 침범 캡
            raw = seg_dir / f"seg_{i:04d}_raw{ext}"
            raw.write_bytes(data)
            _fit_audio(raw, fp, slot, max_speedup=max_sp, max_len=caps[i])
        else:
            fp.write_bytes(data)
        seg_files.append((seg["start"], fp))

    draft = base / "dub_ja_draft.wav"
    _assemble_timeline(seg_files, draft)
    _normalize_track(draft, float(config.get("dub", {}).get("voice_norm_peak", 0.9)))
    write_json(build_alignment_report(video_id, segments, voice_ref),
               base / "alignment_report.json")
    log.info("더빙 초안(검토 전): %s (세그먼트 %d, backend=%s)", draft, len(segments), backend)
    return {"draft": str(draft), "segments": len(segments), "backend": backend}


# ── 영상→더빙 (ASR → 번역 → 합성 → 믹스) ─────────────────────────────────
def reliable_segment(no_speech_prob: float, avg_logprob: float,
                     max_no_speech: float = 0.5, min_logprob: float = -1.2) -> bool:
    """Whisper 할루시네이션 필터 — 음악/효과음에서 '유료 광고 포함' 류 문구를 지어내는
    세그먼트를 거른다(실측: 아기루피 Short, no_speech 0.75 로 광고 고지문 생성).
    no_speech_prob 높음 = 모델 스스로 '말 아님' / avg_logprob 매우 낮음 = 확신 없음."""
    return no_speech_prob <= max_no_speech and avg_logprob >= min_logprob


def transcribe(media: str, config: dict[str, Any], language: str = "ko") -> list[dict[str, Any]]:
    """faster-whisper 로 음성 받아쓰기 → [{start,end,text}] (대사 없는 영상이면 빈 리스트).

    할루시네이션 세그먼트(reliable_segment 참고)는 제외 — 없는 대사를 더빙하지 않는다."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise ImportError("faster-whisper 필요: pip install faster-whisper") from e
    dconf = config.get("dub", {})
    size = dconf.get("asr_model", "base")
    # 배경음악이 큰 영상은 VAD 가 대사를 통째로 거를 수 있어 config 로 끌 수 있게 함.
    vad = bool(dconf.get("asr_vad_filter", True))
    max_ns = float(dconf.get("asr_max_no_speech", 0.5))
    min_lp = float(dconf.get("asr_min_logprob", -1.2))
    model = WhisperModel(size, device="cpu", compute_type="int8")
    segs, _ = model.transcribe(str(media), language=language, vad_filter=vad)
    out = []
    for s in segs:
        if not s.text.strip():
            continue
        if not reliable_segment(float(s.no_speech_prob), float(s.avg_logprob), max_ns, min_lp):
            log.info("ASR 할루시네이션 의심 제외: %r (no_speech=%.2f, logprob=%.2f)",
                     s.text.strip(), s.no_speech_prob, s.avg_logprob)
            continue
        out.append({"start": float(s.start), "end": float(s.end), "text": s.text.strip()})
    return out


def separate_vocals(media: str, out_dir, config: dict[str, Any]) -> Path:
    """Demucs 2-stem 분리 → no_vocals(반주·효과음) 스템 경로. 원본 목소리 제거용."""
    import subprocess

    out_dir = ensure_dir(out_dir)
    model = config.get("dub", {}).get("demucs_model", "htdemucs")
    nov = out_dir / model / Path(media).stem / "no_vocals.wav"
    if nov.exists():                                   # 이미 분리됨 → 재실행 생략(느린 CPU 절약)
        return nov
    subprocess.run([sys.executable, "-m", "demucs", "--two-stems", "vocals", "-n", model,
                    "-o", str(out_dir), str(media)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not nov.exists():
        raise RuntimeError(f"Demucs no_vocals 스템 없음: {nov}")
    return nov


def loop_plan(duration: float, min_s: float = 3.2, max_s: float = 10.0,
              gap: float = 0.15) -> int:
    """GPT-SoVITS 레퍼런스 3~10초 요건 — 짧은 발화를 몇 번 반복할지(0=사용 불가).

    실측 검증(2026-07-02 loopy_short): "루피" 2.35초 발화를 0.15초 간격 2회 연결해
    4.85초 레퍼런스로 사용, 원본 음색 클로닝 성공."""
    import math
    if duration <= 0.3:                      # 유의미한 발화 아님
        return 0
    if duration >= min_s:
        return 1
    n = math.ceil((min_s + gap) / (duration + gap))
    total = n * duration + (n - 1) * gap
    return n if total <= max_s else 0


def pick_ref_segments(segs: list[dict[str, Any]], max_total: float = 8.0) -> list[dict[str, Any]]:
    """레퍼런스로 쓸 대사 세그먼트 — 앞에서부터 그리디로 합계 max_total 초까지."""
    out, total = [], 0.0
    for s in segs:
        d = max(0.0, float(s.get("end", 0)) - float(s.get("start", 0)))
        if d <= 0:
            continue
        if out and total + d > max_total:
            break
        out.append(s)
        total += d
    return out


def build_self_ref(video: str, segs: list[dict[str, Any]], config: dict[str, Any],
                   out_dir) -> Optional[dict[str, str]]:
    """영상 '자체 목소리'로 GPT-SoVITS 레퍼런스 구축 → {ref_wav, prompt_text} 또는 None.

    음색 은행(config ref)보다 해당 영상 목소리가 항상 더 정확하다(2026-07-02 실측 —
    먹방 레퍼런스로 더빙하자 "루피 목소리가 아니다" 피드백, self-ref 로 원본 피치 일치).
    플로우: demucs 보컬 분리 → 대사 구간 컷·정제 → 3초 미만이면 반복 연결.
    실패(대사 없음·너무 짧음·분리 실패) 시 None — 호출자가 은행 레퍼런스로 폴백."""
    import subprocess
    from engine import common

    picked = pick_ref_segments(segs)
    if not picked:
        return None
    out_dir = ensure_dir(out_dir)
    audio = out_dir / "self_src.wav"
    if common.extract_audio(video, audio) is None:
        return None
    try:
        nov = separate_vocals(str(audio), out_dir / "stems", config)
        voc = Path(nov).parent / "vocals.wav"
    except Exception as e:                    # demucs 미설치·실패 → 폴백
        log.warning("self-ref 보컬 분리 실패(%s) → 은행 레퍼런스 사용", e)
        return None
    # 대사 구간만 이어붙이고 정제(저역 컷·노이즈 감쇠·레벨 정규화), mono 32k
    pad = 0.05
    parts, filters = [], []
    for i, s in enumerate(picked):
        st, en = max(0.0, float(s["start"]) - pad), float(s["end"]) + pad
        filters.append(f"[0:a]atrim={st:.3f}:{en:.3f},asetpts=N/SR/TB[a{i}]")
        parts.append(f"[a{i}]")
    fc = (";".join(filters) + ";" + "".join(parts)
          + f"concat=n={len(picked)}:v=0:a=1,"
          + "highpass=f=60,afftdn=nf=-25,dynaudnorm=p=0.7:m=10[out]")
    seg_wav = out_dir / "self_seg.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(voc), "-filter_complex", fc,
                    "-map", "[out]", "-ac", "1", "-ar", "32000", str(seg_wav)], check=True)
    dur = float(common.probe(seg_wav).get("duration", 0.0) or 0.0)
    n = loop_plan(dur)
    if n == 0:
        log.info("self-ref 발화 부족(%.2fs) → 은행 레퍼런스 사용", dur)
        return None
    ref = seg_wav
    if n > 1:                                 # 0.15s 무음 간격으로 n 회 반복 연결
        ref = out_dir / "self_ref.wav"
        inputs = ["-i", str(seg_wav), "-f", "lavfi", "-t", "0.15",
                  "-i", "anullsrc=r=32000:cl=mono"]
        seq = "".join(["[0:a]" if i % 2 == 0 else "[1:a]" for i in range(2 * n - 1)])
        subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex",
                        f"{seq}concat=n={2 * n - 1}:v=0:a=1", "-ar", "32000", "-ac", "1",
                        str(ref)], check=True)
    text = " ".join(s["text"] for s in picked)
    prompt = " ".join([text] * n)
    log.info("self-ref 레퍼런스: %s (%.2fs x%d, 전사=%r)", ref, dur, n, text[:60])
    return {"ref_wav": str(ref), "prompt_text": prompt}


def _mute_windows(in_path: Path, out_path: Path, windows: list[tuple[float, float]]) -> None:
    """오디오에서 지정 시간창만 음소거(나머지 원본 유지). dialogue_only 시 대사 구간만 제거."""
    import subprocess

    if not windows:
        out_path.write_bytes(Path(in_path).read_bytes())
        return
    expr = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in windows)   # OR(합>0)
    subprocess.run(["ffmpeg", "-y", "-i", str(in_path), "-af", f"volume=0:enable='{expr}'",
                    str(out_path)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _mix_two(a: Path, b: Path, out: Path) -> None:
    """두 오디오 합성(정규화 없이 합산). 반주(no_vocals) + 리액션(대사 제거 보컬)."""
    import subprocess

    subprocess.run(["ffmpeg", "-y", "-i", str(a), "-i", str(b), "-filter_complex",
                    "amix=inputs=2:duration=longest:normalize=0", str(out)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def dub_from_video(video_id: str, video: str, level: str, config: dict[str, Any],
                   speaker_wav: Optional[str] = None, source_lang: str = "ko",
                   mux: bool = True) -> dict[str, Any]:
    """대사 있는 영상 풀 더빙: 받아쓰기(ASR) → 트랜스크리에이션 → 클론 합성 → 영상에 믹스.

    [필수 게이트] Level C 한정. 결과는 초안 — retention·hero 는 사람/성우 검토.
    """
    require_level_c(level)
    from engine import common as _common
    from engine import render as render_mod
    from engine.translate import transcreate

    segs = transcribe(video, config, language=source_lang)
    if not segs:
        raise ValueError("받아쓰기된 대사 없음 — 대사 없는 영상(ASMR 등)일 수 있음. 대사 있는 영상 필요.")
    if config.get("dub", {}).get("dialogue_only", False):
        kept = [s for s in segs if _is_dialogue(s["text"])]
        log.info("dialogue_only: ASR %d개 중 실제 대사 %d개만 더빙(리액션/씹는소리는 원본 유지)",
                 len(segs), len(kept))
        segs = kept
        if not segs:
            raise ValueError("dialogue_only 필터 결과 대사 0개. asr_model 또는 필터 확인.")
    log.info("ASR 대사 %d개 받아쓰기 완료 → 트랜스크리에이션", len(segs))

    entries = transcreate([s["text"] for s in segs], config)   # 한국어→일본어(LLM, persona)
    jmap = {e.source: e.target for e in entries}
    events = [{"start": s["start"], "end": s["end"], "text": jmap.get(s["text"], "")} for s in segs]

    base = ensure_dir(resolve_path(f"{config['paths']['outputs_dir']}/{video_id}"))

    # self-ref: 이 영상의 원본 목소리를 레퍼런스로(음색 은행보다 정확) — 실패 시 은행 폴백
    gsv = config.get("dub", {}).get("gptsovits", {})
    if dub_backend(config) == "gptsovits" and gsv.get("self_ref", True):
        sref = build_self_ref(video, segs, config, base / "ref")
        if sref:
            import copy
            cand = copy.deepcopy(config)
            g = cand["dub"]["gptsovits"]
            g["ref_wav"], g["prompt_text"] = sref["ref_wav"], sref["prompt_text"]
            g["prompt_lang"], g["aux_refs"] = "ko", []
            # 사전 프로브(⚠ 반드시 서브프로세스): 퇴화 self-ref 는 프로세스 내
            # 모델 캐시를 오염시켜 이후 '은행 ref 포함 모든' 합성을 무음으로 만든다
            # (2026-07-08 실측 — 모듈 리셋도 무효). 격리 프로브 통과 시에만 채택.
            import subprocess as _sp
            res = _sp.run([sys.executable, "-m", "src.dub",
                           f"--probe-ref={sref['ref_wav']}",
                           f"--prompt-text={sref['prompt_text']}"],
                          capture_output=True, text=True, timeout=600,
                          cwd=str(resolve_path(".")))
            if res.returncode == 0:
                config = cand
                log.info("self-ref 프로브 통과(격리) → 채택")
            else:
                log.warning("self-ref 프로브 실패 → 은행 레퍼런스(%s) 사용: %s",
                            gsv.get("ref_wav"), (res.stdout + res.stderr)[-120:])

    ja_srt = base / "ja_dub.srt"
    ja_srt.write_text(render_mod.build_srt(events, int(config.get("render", {}).get("line_max_chars", 26))),
                      encoding="utf-8")
    res = dub(video_id, str(ja_srt), level, config, speaker_wav=speaker_wav)

    if mux:
        dconf = config.get("dub", {})
        bg = float(dconf.get("bg_volume", 0.3))
        bg_audio = None
        if dconf.get("remove_original_vocals", False):
            nov = separate_vocals(video, base / "stems", config)            # 반주/효과음 스템
            if dconf.get("dialogue_only", False):
                # 대사 구간의 보컬만 제거(일본어 더빙으로 교체) + 리액션/씹는소리는 원본 유지.
                voc = Path(nov).parent / "vocals.wav"
                _mute_windows(voc, base / "reactions.wav",
                              [(s["start"], s["end"]) for s in segs])
                _mix_two(nov, base / "reactions.wav", base / "bg_reactions_mix.wav")
                bg_audio = str(base / "bg_reactions_mix.wav")
                bg = max(bg, 0.85)                                           # 리액션/ASMR 또렷하게
                log.info("dialogue_only: 대사 구간만 원본 제거, 리액션/씹는소리 보존")
            else:
                bg_audio = str(nov)
                bg = max(bg, 0.4)                                            # ASMR/반주 보존
                log.info("원본 보컬 제거(Demucs) → 반주 스템 믹스")
        # ASMR 다이내믹 보존: loudnorm 대신 limiter 로 피크만 제한(째짐 방지)
        out = _common.mux_dub(video, res["draft"], base / "final_dubbed.mp4",
                              bg_volume=bg, voice_volume=float(dconf.get("voice_volume", 1.1)),
                              bg_audio=bg_audio, loudnorm=bool(dconf.get("loudnorm", False)),
                              limiter=bool(dconf.get("limiter", True)),
                              limit=float(dconf.get("peak_limit", 0.97)))
        res["dubbed_video"] = str(out)
        log.info("더빙 영상(초안): %s", out)
        # 화면에 한국어 자막(번인 텍스트)이 없는 영상 → 더빙된 일본어 오디오에 맞춘
        # 일본어 자막을 번인(시청자가 대사를 읽을 수 있게). ASR 타이밍 그대로 사용.
        if dconf.get("burn_dub_subtitle", True):
            meta = _common.probe(video)
            ja_ass = base / "ja_dub.ass"
            ja_ass.write_text(
                render_mod.build_ass(events, meta["width"], meta["height"],
                                     int(config.get("render", {}).get("line_max_chars", 26))),
                encoding="utf-8")
            subbed = _common.burn_subtitles(
                str(out), str(ja_ass), base / "final_dubbed_subbed.mp4",
                fonts_dir=str(resolve_path(config["paths"]["fonts_dir"])))
            res["dubbed_video_subbed"] = str(subbed)
            log.info("일본어 더빙 자막 번인: %s", subbed)
    return res


def _fit_audio(in_path: Path, out_path: Path, target_sec: float, max_speedup: float = 1.6,
               max_len: Optional[float] = None) -> None:
    """합성 음성을 슬롯 길이에 맞게 time-stretch(피치 유지). 과도한 변형은 클램프.

    max_len 지정 시: 배속 후에도 그 길이를 넘으면 잘라내고 끝에 짧은 페이드아웃.
    → 한 세그의 환각/과길이가 '다음 발화' 위로 겹쳐 깔리는 드론을 구조적으로 차단.
    """
    import subprocess

    dur = common.probe(in_path).get("duration", 0.0)
    if dur <= 0 or target_sec <= 0:
        out_path.write_bytes(in_path.read_bytes())
    else:
        speed = _fit_speed(dur, target_sec, max_speedup)
        if abs(speed - 1.0) < 0.05:                # 충분히 근접 → 그대로
            out_path.write_bytes(in_path.read_bytes())
        else:
            subprocess.run(["ffmpeg", "-y", "-i", str(in_path), "-filter:a",
                            atempo_filters(speed), str(out_path)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if max_len is not None:                         # 침범 방지 캡
        cur = common.probe(out_path).get("duration", 0.0)
        if _needs_truncate(cur, max_len):
            tmp = out_path.with_suffix(".cap" + out_path.suffix)
            fade_st = max(0.0, max_len - 0.12)
            subprocess.run(["ffmpeg", "-y", "-i", str(out_path), "-t", f"{max_len:.3f}",
                            "-af", f"afade=t=out:st={fade_st:.3f}:d=0.12", str(tmp)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            tmp.replace(out_path)


def _assemble_timeline(seg_files: list[tuple[float, Path]], out: Path) -> None:
    """각 세그먼트를 시작 시각에 배치해 한 트랙으로 mix(ffmpeg adelay+amix)."""
    if not seg_files:
        log.warning("세그먼트 없음 → 더빙 트랙 생략")
        return
    if not common.has_ffmpeg():
        raise RuntimeError("ffmpeg 필요(더빙 트랙 합성).")
    import subprocess

    cmd: list[str] = ["ffmpeg", "-y"]
    for _, fp in seg_files:
        cmd += ["-i", str(fp)]
    parts, labels = [], []
    for idx, (start, _) in enumerate(seg_files):
        ms = int(start * 1000)
        parts.append(f"[{idx}]adelay={ms}|{ms}[a{idx}]")
        labels.append(f"[a{idx}]")
    filt = ";".join(parts) + ";" + "".join(labels) + \
        f"amix=inputs={len(seg_files)}:normalize=0[out]"
    cmd += ["-filter_complex", filt, "-map", "[out]", str(out)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _normalize_track(wav: Path, target_peak: float = 0.9) -> None:
    """보이스 트랙 피크 정규화(헤드룸 확보). amix 합산 후 클리핑/limiter 펌핑 완화."""
    try:
        import numpy as np
        import soundfile as sf
    except ImportError:                              # 의존성 없으면 건너뜀(원본 유지)
        return
    a, sr = sf.read(str(wav))
    peak = float(np.max(np.abs(a))) if a.size else 0.0
    scale = _norm_scale(peak, target_peak)
    if abs(scale - 1.0) > 1e-3:
        sf.write(str(wav), a * scale, sr)


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Level C 더빙 초안(드래프트 오디오까지)")
    p.add_argument("--video-id", required=True)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--subtitle", help="ja.srt/ja.ass (자막 기반 더빙)")
    src.add_argument("--video", help="대사 있는 영상 (ASR→번역→더빙 풀 플로우)")
    p.add_argument("--level", default="C", help="C 가 아니면 거부")
    p.add_argument("--backend", default=None, help="xtts(오픈소스 클로닝) | elevenlabs")
    p.add_argument("--speaker", default=None, help="xtts: 클로닝용 음성 샘플(wav/mp3) 경로")
    p.add_argument("--voice", default=None, help="elevenlabs: voice_id")
    p.add_argument("--source-lang", default="ko", help="--video ASR 원본 언어")
    p.add_argument("--config", default=None)
    return p.parse_args(argv)


def _probe_ref_main(argv: list[str]) -> None:
    """`--probe-ref` 모드 — 레퍼런스 1개를 시험 합성해 exit 0(정상)/1(무음성 퇴화).

    반드시 별도 프로세스로 호출할 것: 퇴화 레퍼런스는 프로세스 내 모델 캐시를
    오염시켜 이후 모든 합성을 무음으로 만든다(2026-07-08 실측 — 모듈 리셋으로도
    복구 불가, 하위 모듈 캐시 잔존). 격리가 유일하게 확실한 방역."""
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--probe-ref", required=True)
    p.add_argument("--prompt-text", required=True)
    p.add_argument("--config", default=None)
    a = p.parse_args(argv)
    config = load_config(a.config)
    import copy
    cfg = copy.deepcopy(config)
    g = cfg["dub"]["gptsovits"]
    g["ref_wav"], g["prompt_text"], g["prompt_lang"] = a.probe_ref, a.prompt_text, "ko"
    g["aux_refs"], g["pitch_match_tries"], g["retry_tries"] = [], 1, 1
    try:
        _synthesize_gptsovits("ルーピー", "ja", cfg)
        print("PROBE_OK")
        sys.exit(0)
    except Exception as e:
        print(f"PROBE_FAIL: {str(e)[:120]}")
        sys.exit(1)


def main(argv: Optional[list[str]] = None) -> None:
    argv = list(sys.argv[1:]) if argv is None else argv
    if any(a.startswith("--probe-ref") for a in argv):
        _probe_ref_main(argv)
        return
    args = _parse_args(argv)
    config = load_config(args.config)
    if args.backend:
        config.setdefault("dub", {})["tts_backend"] = args.backend
    if args.video:
        dub_from_video(args.video_id, args.video, args.level, config,
                       speaker_wav=args.speaker, source_lang=args.source_lang)
    else:
        dub(args.video_id, args.subtitle, args.level, config,
            voice_id=args.voice, speaker_wav=args.speaker)


if __name__ == "__main__":
    main()
