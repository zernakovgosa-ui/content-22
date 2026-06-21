# -*- coding: utf-8 -*-
"""Speech-to-text for the clip pipeline (render_mode="clip").

Extracts the audio track from a local video with ffmpeg, then transcribes it
with **word/segment timestamps** so the clip engine can (a) let the LLM pick the
best moments and (b) burn subtitles onto each clip.

Providers (chosen via settings["clip_transcriber"]):
  - "groq"      → Groq Whisper Large v3 Turbo. FREE tier, ~216x realtime, word
                  timestamps. OpenAI-compatible endpoint → pure stdlib urllib.
  - "openai"    → OpenAI whisper-1 (same multipart shape).
  - "whispercpp"→ fully-offline local binary (no API key), if configured.

Design rules (match the rest of TREZZY):
  - stdlib only (urllib) for HTTP — runs on Windows with no extra pip installs.
  - NEVER raise to the caller. On any failure return an empty transcript
    ({"ok": False, "segments": []}); the clip pipeline then falls back to naive
    even-spaced cuts without captions. The pipeline must never break.

Public API:
    transcribe(source_path, settings) -> {
        "ok": bool,
        "text": str,
        "segments": [{"start": float, "end": float, "text": str}],
        "words":    [{"start": float, "end": float, "word": str}],   # may be empty
        "provider": str,
        "error":    Optional[str],
        "duration": float,   # media length (= longest segment/word end); 2nd ffmpeg pass avoidable
        "cached":   bool,    # True when served from the on-disk transcript cache
    }
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import urllib.request
import urllib.error

from .local_renderer import _find_ffmpeg  # reuse the cross-platform ffmpeg locator

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"
OPENAI_MODEL = "whisper-1"

MAX_UPLOAD_BYTES = 24 * 1024 * 1024   # Whisper API hard limit is 25MB; stay under.
SEGMENT_SECONDS = 600                  # split long audio into 10-min chunks for upload.

CACHE_VERSION = 3                      # bump when result shape changes → old cache ignored.
                                       # v3: multi-key rotation + sequential long-video STT
                                       # (старые ЧАСТИЧНЫЕ транскрипты больше не отдаём из кэша)


# ----------------------------------------------------------------------------
# Transcript cache (re-running the SAME video costs ~0s instead of re-uploading)
# ----------------------------------------------------------------------------
def _cache_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "trezzy_stt_cache"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _cache_key(source_path: Path, attempts: List[str], settings: Dict[str, Any]) -> Optional[str]:
    """Stable key from the file identity + what would change the result.

    Keyed on absolute path, size and mtime (so editing/replacing the file busts
    the cache) plus the provider order and models. Returns None if the file can't
    be stat'd (then we just skip caching — never break the pipeline).
    """
    try:
        st = source_path.stat()
    except Exception:
        return None
    import hashlib
    sig = "|".join([
        str(CACHE_VERSION),
        str(source_path.resolve()),
        str(st.st_size),
        str(int(st.st_mtime)),
        ",".join(attempts),
        GROQ_MODEL, OPENAI_MODEL,
        str(settings.get("whispercpp_model") or ""),
    ])
    return hashlib.sha1(sig.encode("utf-8", errors="replace")).hexdigest()


def _cache_load(key: Optional[str]) -> Optional[Dict[str, Any]]:
    if not key:
        return None
    p = _cache_dir() / f"{key}.json"
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("segments") or data.get("text"):
                data["cached"] = True
                return data
    except Exception:
        pass
    return None


def _cache_store(key: Optional[str], result: Dict[str, Any]) -> None:
    if not key:
        return
    try:
        p = _cache_dir() / f"{key}.json"
        p.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _transcript_duration(result: Dict[str, Any]) -> float:
    """Longest segment/word end = media duration. Lets callers skip a 2nd ffmpeg pass."""
    end = 0.0
    for s in result.get("segments") or []:
        try:
            end = max(end, float(s.get("end") or 0.0))
        except Exception:
            pass
    for w in result.get("words") or []:
        try:
            end = max(end, float(w.get("end") or 0.0))
        except Exception:
            pass
    return round(end, 3)


# ----------------------------------------------------------------------------
# Audio extraction (ffmpeg)
# ----------------------------------------------------------------------------
def _extract_audio(source_path: Path, workdir: Path) -> List[Tuple[Path, float]]:
    """Extract a compact mono 16kHz mp3 from the video.

    Returns a list of (audio_chunk_path, offset_seconds). One element for short
    videos; several for long ones (so each upload stays under the 25MB limit).
    Raises RuntimeError if ffmpeg is missing or extraction fails.
    """
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found (needed to extract audio for transcription).")

    full = workdir / "audio_full.mp3"
    cmd = [
        ffmpeg, "-y", "-i", str(source_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k",
        str(full),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0 or not full.exists():
        err = proc.stderr.decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(f"ffmpeg audio extraction failed: {err}")

    if full.stat().st_size <= MAX_UPLOAD_BYTES:
        return [(full, 0.0)]

    # Too big for a single upload → split into fixed-length chunks.
    chunks_dir = workdir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    seg_cmd = [
        ffmpeg, "-y", "-i", str(full),
        "-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
        "-reset_timestamps", "1", "-c", "copy",
        str(chunks_dir / "chunk_%03d.mp3"),
    ]
    sp = subprocess.run(seg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if sp.returncode != 0:
        # Fall back to the single (oversized) file — the API may still accept it.
        return [(full, 0.0)]

    out: List[Tuple[Path, float]] = []
    for i, chunk in enumerate(sorted(chunks_dir.glob("chunk_*.mp3"))):
        out.append((chunk, float(i * SEGMENT_SECONDS)))
    return out or [(full, 0.0)]


# ----------------------------------------------------------------------------
# OpenAI-compatible transcription (Groq + OpenAI)
# ----------------------------------------------------------------------------
def _build_multipart(
    text_fields: List[Tuple[str, str]],
    file_name: str,
    file_bytes: bytes,
    file_field: str = "file",
    mime: str = "audio/mpeg",
) -> Tuple[bytes, str]:
    """Build a minimal multipart/form-data body (supports repeated field names)."""
    boundary = "----trezzy" + uuid.uuid4().hex
    chunks: List[bytes] = []
    for name, value in text_fields:
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
    )
    body = b"".join(chunks) + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return body, f"multipart/form-data; boundary={boundary}"


def _transcribe_openai_compatible(
    url: str, api_key, model: str, audio_path: Path, timeout: int = 300,
    start_ki: int = 0,
) -> Dict[str, Any]:
    """Call an OpenAI-compatible /audio/transcriptions endpoint; return parsed JSON.

    api_key может быть СТРОКОЙ или СПИСКОМ ключей: при 429 (лимит Groq) сразу
    пробуем следующий ключ — это и есть авто-ротация для длинных видео.
    """
    keys = [k for k in (api_key if isinstance(api_key, (list, tuple)) else [api_key]) if k]
    if not keys:
        raise RuntimeError("transcription: нет ключа")
    file_bytes = audio_path.read_bytes()
    fields = [
        ("model", model),
        ("response_format", "verbose_json"),
        ("timestamp_granularities[]", "segment"),
        ("timestamp_granularities[]", "word"),
        ("temperature", "0"),
    ]
    body, ctype = _build_multipart(fields, audio_path.name, file_bytes)
    last_err: Optional[str] = None
    ki = start_ki
    tried_keys = 0          # сколько РАЗНЫХ ключей перебрали в текущем круге
    max_tries = max(3, len(keys) * 2 + 2)
    for attempt in range(1, max_tries + 1):
        key = keys[ki % len(keys)]
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": ctype,
                "Accept": "application/json",
                # Cloudflare (Groq/OpenAI) 403s the default urllib UA — send a browser one.
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            if e.code in (429, 401, 403):              # лимит ИЛИ битый/запрещённый ключ → следующий
                tag = "лимит" if e.code == 429 else "битый ключ"
                last_err = f"HTTP {e.code} (ключ {ki % len(keys) + 1}/{len(keys)}, {tag})"
                ki += 1
                tried_keys += 1
                if tried_keys < len(keys):             # ещё есть неиспробованные ключи — без паузы
                    print(f"[stt] {tag} (HTTP {e.code}) — пробую следующий Groq-ключ", flush=True)
                    continue
                # обошли ВСЕ ключи (корректно при любом start_ki)
                if e.code == 429:                      # все в лимите — пауза, новый круг
                    print("[stt] все ключи в лимите — пауза перед новым кругом", flush=True)
                    time.sleep(8)
                    tried_keys = 0
                    continue
                raise RuntimeError(f"все Groq-ключи битые (HTTP {e.code}): {detail or e.reason}")
            if e.code in (500, 502, 503, 504) and attempt < max_tries:
                last_err = f"HTTP {e.code}"
                time.sleep(6)
                continue
            raise RuntimeError(f"transcription HTTP {e.code}: {detail or e.reason}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = str(e)
            if attempt < max_tries:
                print(f"[stt] сеть моргнула ({str(e)[:80]}), попытка {attempt + 1}/{max_tries}...", flush=True)
                time.sleep(6)
                continue
    raise RuntimeError(f"transcription unreachable after {max_tries} tries: {last_err}")


def _parse_openai_json(res: Dict[str, Any], offset: float) -> Tuple[str, List[Dict], List[Dict]]:
    """Pull (text, segments, words) out of a verbose_json response, applying a time offset."""
    text = (res.get("text") or "").strip()
    segments: List[Dict] = []
    for s in res.get("segments") or []:
        try:
            segments.append({
                "start": float(s.get("start", 0.0)) + offset,
                "end": float(s.get("end", 0.0)) + offset,
                "text": (s.get("text") or "").strip(),
            })
        except Exception:
            continue
    words: List[Dict] = []
    for w in res.get("words") or []:
        try:
            words.append({
                "start": float(w.get("start", 0.0)) + offset,
                "end": float(w.get("end", 0.0)) + offset,
                "word": (w.get("word") or w.get("text") or "").strip(),
            })
        except Exception:
            continue
    return text, segments, words


CHUNK_UPLOAD_WORKERS = 3      # параллель только для коротких (2-3 куска)
SEQ_CHUNK_THRESHOLD = 3       # больше кусков → ПОСЛЕДОВАТЕЛЬНО (бережём rate-limit Groq)
CHUNK_SPACING_S = 1.5         # пауза между последовательными кусками


def _transcribe_cloud(
    url: str, api_key: str, model: str, chunks: List[Tuple[Path, float]]
) -> Dict[str, Any]:
    """Transcribe every audio chunk and merge results with offsets applied.

    Длинное видео (фильм/час+) бьётся на 10-минутные куски. Раньше грузили их
    ПАРАЛЛЕЛЬНО — но на бесплатном Groq это ловит rate-limit, часть кусков
    отваливалась, и из часа распознавалось ~7 минут (→ субтитры только в начале).
    Теперь: ≤3 кусков — параллельно (быстро), >3 — ПОСЛЕДОВАТЕЛЬНО с паузой (больше
    проходит). Падение одного куска НЕ роняет остальные — берём что распозналось.
    """
    n = len(chunks)

    def _one(idx: int, audio_path: Path, offset: float) -> Tuple[int, str, List[Dict], List[Dict]]:
        try:
            res = _transcribe_openai_compatible(url, api_key, model, audio_path, start_ki=idx)
            text, segments, words = _parse_openai_json(res, offset)
            return idx, text, segments, words
        except Exception as e:
            print(f"[stt] кусок {idx + 1}/{n} не распознан: {str(e)[:80]}", flush=True)
            return idx, "", [], []

    nkeys = len(api_key) if isinstance(api_key, (list, tuple)) else 1
    results: List[Optional[Tuple[int, str, List[Dict], List[Dict]]]] = [None] * n
    if n <= 1:
        if chunks:
            results[0] = _one(0, chunks[0][0], chunks[0][1])
    else:
        # Параллелим ВСЕ куски: ключей много → по числу ключей (каждый кусок стартует
        # со СВОЕГО ключа через start_ki=idx, лимит одного ключа не ловим). Один ключ →
        # скромная параллель, чтоб не упереться в его rate-limit.
        workers = min(n, (nkeys if nkeys > 1 else CHUNK_UPLOAD_WORKERS), 6)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futs = {pool.submit(_one, i, ap, off): i for i, (ap, off) in enumerate(chunks)}
            for fut in concurrent.futures.as_completed(futs):
                r = fut.result()
                results[r[0]] = r

    all_text: List[str] = []
    all_segments: List[Dict] = []
    all_words: List[Dict] = []
    got = 0
    for item in results:
        if item is None:
            continue
        _, text, segments, words = item
        if segments or text:
            got += 1
        if text:
            all_text.append(text)
        all_segments.extend(segments)
        all_words.extend(words)
    if n > 1:
        print(f"[stt] распознано кусков: {got}/{n}", flush=True)
    return {
        "ok": True,
        "text": " ".join(all_text).strip(),
        "segments": all_segments,
        "words": all_words,
        "chunks_total": n,
        "chunks_ok": got,        # got<n → транскрипт ЧАСТИЧНЫЙ (не кэшировать как финал)
    }


# ----------------------------------------------------------------------------
# whisper.cpp (offline, optional)
# ----------------------------------------------------------------------------
def _transcribe_whispercpp(
    source_path: Path, workdir: Path, settings: Dict[str, Any]
) -> Dict[str, Any]:
    """Run a local whisper.cpp binary. Requires settings:
        whispercpp_bin   — path to the whisper.cpp executable
        whispercpp_model — path to a .bin/.gguf model file
    """
    binp = settings.get("whispercpp_bin") or os.getenv("WHISPERCPP_BIN") or ""
    model = settings.get("whispercpp_model") or os.getenv("WHISPERCPP_MODEL") or ""
    if not (binp and model and Path(binp).exists() and Path(model).exists()):
        raise RuntimeError("whisper.cpp not configured (set whispercpp_bin + whispercpp_model).")

    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found (needed to make a 16kHz wav for whisper.cpp).")
    wav = workdir / "audio_16k.wav"
    ex = subprocess.run(
        [ffmpeg, "-y", "-i", str(source_path), "-vn", "-ac", "1", "-ar", "16000", str(wav)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if ex.returncode != 0 or not wav.exists():
        raise RuntimeError("ffmpeg wav extraction for whisper.cpp failed.")

    out_base = workdir / "wcpp_out"
    proc = subprocess.run(
        [binp, "-m", model, "-f", str(wav), "-oj", "-of", str(out_base)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    json_path = Path(str(out_base) + ".json")
    if proc.returncode != 0 or not json_path.exists():
        raise RuntimeError("whisper.cpp run failed or produced no JSON.")

    data = json.loads(json_path.read_text(encoding="utf-8"))
    segments: List[Dict] = []
    texts: List[str] = []
    for item in data.get("transcription") or []:
        try:
            off = item.get("offsets") or {}
            start = float(off.get("from", 0)) / 1000.0
            end = float(off.get("to", 0)) / 1000.0
            txt = (item.get("text") or "").strip()
            if txt:
                segments.append({"start": start, "end": end, "text": txt})
                texts.append(txt)
        except Exception:
            continue
    return {"ok": True, "text": " ".join(texts).strip(), "segments": segments, "words": []}


# ----------------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------------
def transcribe(source_path: str | Path, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Transcribe a local video. Never raises — returns ok=False on failure."""
    settings = settings or {}
    source_path = Path(source_path)
    mode = (settings.get("clip_transcriber") or "groq").lower()
    # Несколько Groq-ключей → авто-ротация при лимите (часовое видео упирается в
    # квоту одного ключа). Список groq_api_keys + одиночный groq_api_key, без дублей.
    _graw = settings.get("groq_api_keys") or []
    if isinstance(_graw, str):
        _graw = _graw.split(",")
    groq_keys: List[str] = []
    for k in list(_graw) + [settings.get("groq_api_key") or os.getenv("GROQ_API_KEY") or ""]:
        k = (k or "").strip()
        if k and k not in groq_keys:
            groq_keys.append(k)
    openai_key = settings.get("openai_api_key") or os.getenv("OPENAI_API_KEY") or ""

    if not source_path.exists():
        return {"ok": False, "text": "", "segments": [], "words": [],
                "provider": "none", "error": f"source not found: {source_path}"}

    # Build an ordered list of (provider, runner) to try. First success wins.
    attempts: List[str] = []
    if mode == "whispercpp":
        attempts = ["whispercpp", "groq", "openai"]
    elif mode == "openai":
        attempts = ["openai", "groq", "whispercpp"]
    else:  # "groq" (default) or anything unknown
        attempts = ["groq", "openai", "whispercpp"]

    # Кэш: тот же файл + те же провайдеры/модели → отдаём готовый транскрипт,
    # не качая аудио и не дёргая Groq снова (важно при повторных прогонах и ретраях).
    use_cache = settings.get("transcript_cache", True)
    cache_key = _cache_key(source_path, attempts, settings) if use_cache else None
    cached = _cache_load(cache_key)
    if cached is not None:
        print("[stt] транскрипт из кэша (распознавание пропущено)", flush=True)
        return cached

    errors: List[str] = []
    workdir = Path(tempfile.mkdtemp(prefix="trezzy_stt_"))
    try:
        # Cloud providers share one audio extraction; whisper.cpp does its own (wav).
        cloud_chunks: Optional[List[Tuple[Path, float]]] = None

        def _chunks() -> List[Tuple[Path, float]]:
            nonlocal cloud_chunks
            if cloud_chunks is None:
                cloud_chunks = _extract_audio(source_path, workdir)
            return cloud_chunks

        for provider in attempts:
            try:
                if provider == "groq":
                    if not groq_keys:
                        continue
                    out = _transcribe_cloud(GROQ_URL, groq_keys, GROQ_MODEL, _chunks())
                elif provider == "openai":
                    if not openai_key:
                        continue
                    out = _transcribe_cloud(OPENAI_URL, openai_key, OPENAI_MODEL, _chunks())
                elif provider == "whispercpp":
                    out = _transcribe_whispercpp(source_path, workdir, settings)
                else:
                    continue
                if out.get("segments") or out.get("text"):
                    out["provider"] = provider
                    out["error"] = None
                    out["duration"] = _transcript_duration(out)
                    out["cached"] = False
                    # Кэшируем ТОЛЬКО полный транскрипт. Частичный (chunks_ok<total —
                    # часть кусков не распозналась из-за лимита) отдаём пайплайну, но
                    # НЕ сохраняем — чтобы следующий прогон добрал недостающее, а не
                    # навсегда возвращал обрезанные субтитры из кэша.
                    ct = int(out.get("chunks_total") or 1)
                    ck = int(out.get("chunks_ok") or 1)
                    out["partial"] = ck < ct
                    if not out["partial"]:
                        _cache_store(cache_key, out)
                    else:
                        print(f"[stt] транскрипт частичный ({ck}/{ct} кусков) — "
                              f"не кэширую, следующий прогон добёрет", flush=True)
                    return out
                errors.append(f"{provider}: empty result")
            except Exception as e:
                errors.append(f"{provider}: {e}")
                continue

        return {"ok": False, "text": "", "segments": [], "words": [],
                "provider": "none", "error": "; ".join(errors) or "no transcriber available"}
    finally:
        try:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass


__all__ = ["transcribe"]
