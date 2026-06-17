# -*- coding: utf-8 -*-
"""Telegram review bot — official Bot API only (rule #3: no login/password posting).

Flow:
  1. After clips render, each one is sent to the owner's chat with inline
     ✅ Опубликовать / ❌ Отклонить buttons (send_clip_for_review).
  2. A daemon poller inside the API process (start_review_poller) long-polls
     getUpdates; on a button tap it:
        ✅ → copies the clip to output/approved/ + appends to data/publish_queue.json
        ❌ → marks it rejected
     and edits the Telegram message so the decision is visible in chat.

State files (data/): tg_pending.json (token → clip), tg_offset.json (update
offset), publish_queue.json (approved queue for the future auto-poster).

Pure stdlib urllib; callback_data stays short (8-char token) because Telegram
caps it at 64 bytes and our job ids are long Cyrillic strings. Telegram bots
can upload ≤50MB, so oversized clips get a compressed preview sent instead
(the approved ORIGINAL on disk is untouched).
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import urllib.request
import urllib.error

API = "https://api.telegram.org/bot{token}/{method}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
MAX_TG_BYTES = 48 * 1024 * 1024   # stay under Telegram's 50MB bot upload cap

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "data"
PENDING_PATH = DATA / "tg_pending.json"
OFFSET_PATH = DATA / "tg_offset.json"
QUEUE_PATH = DATA / "publish_queue.json"
APPROVED_DIR = REPO / "output" / "approved"

_LOCK = threading.Lock()


# ----------------------------------------------------------------- low level
def _call(token: str, method: str, payload: Dict[str, Any], timeout: int = 35) -> Dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        API.format(token=token, method=method), data=data, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _send_video(token: str, chat_id: str, video: Path, caption: str,
                reply_markup: Optional[Dict] = None, timeout: int = 300) -> Dict[str, Any]:
    boundary = "----trezzy" + uuid.uuid4().hex
    fields: List[Tuple[str, str]] = [
        ("chat_id", str(chat_id)),
        ("caption", caption[:1024]),
        ("supports_streaming", "true"),
    ]
    if reply_markup:
        fields.append(("reply_markup", json.dumps(reply_markup, ensure_ascii=False)))
    body = bytearray()
    for k, v in fields:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode("utf-8")
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"video\"; "
             f"filename=\"{video.name}\"\r\nContent-Type: video/mp4\r\n\r\n").encode("utf-8")
    body += video.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        API.format(token=token, method="sendVideo"), data=bytes(body), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ----------------------------------------------------------------- state I/O
def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _tg_creds(settings: Dict[str, Any]) -> Tuple[str, str]:
    token = (settings.get("telegram_bot_token") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = str(settings.get("telegram_chat_id") or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    return token, chat


def review_enabled(settings: Dict[str, Any]) -> bool:
    token, chat = _tg_creds(settings)
    return bool(token and chat)


# ----------------------------------------------------------------- send side
def _preview_if_oversized(video: Path) -> Tuple[Path, bool]:
    """Telegram bots can't upload >50MB → make a 720p preview copy to send."""
    try:
        if video.stat().st_size <= MAX_TG_BYTES:
            return video, False
        from packages.video.local_renderer import _find_ffmpeg
        import subprocess, tempfile
        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            return video, False
        prev = Path(tempfile.gettempdir()) / f"tgprev_{video.stem}.mp4"
        subprocess.run(
            [ffmpeg, "-y", "-i", str(video), "-vf", "scale=720:-2",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(prev)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900,
        )
        if prev.exists() and prev.stat().st_size <= MAX_TG_BYTES:
            return prev, True
    except Exception:
        pass
    return video, False


def send_clip_for_review(settings: Dict[str, Any], clip: Dict[str, Any],
                         job_id: str, topic: str = "") -> bool:
    """Send one rendered clip to the owner with ✅/❌ buttons. Never raises."""
    token, chat = _tg_creds(settings)
    if not (token and chat):
        return False
    try:
        path = Path(clip["path"])
        if not path.exists():
            return False
        key = uuid.uuid4().hex[:8]
        score = clip.get("score")
        cap_lines = [f"🎬 {clip.get('title') or topic or path.stem}"]
        if isinstance(score, (int, float)):
            cap_lines.append(f"⭐ Виральность: {int(score)}/100")
        if clip.get("hook"):
            cap_lines.append(f"🪝 Хук: «{str(clip['hook'])[:90]}»")
        cap_lines.append(f"⏱ {clip.get('duration')}с ({clip.get('start')}–{clip.get('end')}с исходника)")
        if clip.get("reason"):
            cap_lines.append(f"💡 {str(clip['reason'])[:120]}")
        cap_lines.append(f"📦 {job_id} / {path.name}")
        caption = "\n".join(cap_lines)

        markup = {"inline_keyboard": [[
            {"text": "✅ Опубликовать", "callback_data": f"ap:{key}"},
            {"text": "❌ Отклонить", "callback_data": f"rj:{key}"},
        ]]}

        send_path, is_preview = _preview_if_oversized(path)
        if is_preview:
            caption += "\n⚠️ превью сжато (оригинал >50МБ остаётся на диске)"
        res = _send_video(token, chat, send_path, caption, markup)
        if is_preview:
            try:
                send_path.unlink()
            except Exception:
                pass
        if not res.get("ok"):
            print("[tg] sendVideo failed:", str(res)[:200])
            return False

        with _LOCK:
            pending = _read_json(PENDING_PATH, {})
            pending[key] = {
                "job_id": job_id,
                "path": str(path),
                "title": clip.get("title") or "",
                "score": score,
                "caption_file": str(path.with_suffix("")) + ".caption.txt",
                "chat_id": chat,
                "message_id": (res.get("result") or {}).get("message_id"),
                "caption": caption,
                "sent_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            _write_json(PENDING_PATH, pending)
        return True
    except Exception as e:
        print("[tg] send_clip_for_review error:", repr(e)[:200])
        return False


def notify_clips_for_review(settings: Dict[str, Any], clips: List[Dict[str, Any]],
                            job_id: str, topic: str = "") -> int:
    """Send every rendered clip for approval; returns how many were sent."""
    sent = 0
    for c in clips or []:
        if send_clip_for_review(settings, c, job_id, topic):
            sent += 1
    return sent


# --------------------------------------------------------------- decide side
def _approve(entry: Dict[str, Any]) -> str:
    src = Path(entry["path"])
    APPROVED_DIR.mkdir(parents=True, exist_ok=True)
    dest = APPROVED_DIR / f"{entry['job_id']}__{src.name}"
    try:
        if src.exists():
            shutil.copyfile(src, dest)
            cap = Path(entry.get("caption_file") or "")
            if cap.exists():
                shutil.copyfile(cap, dest.with_suffix(".caption.txt"))
    except Exception as e:
        return f"копирование не удалось: {e}"
    with _LOCK:
        queue = _read_json(QUEUE_PATH, [])
        queue.append({
            "id": uuid.uuid4().hex[:10],
            "job_id": entry["job_id"],
            "clip": src.name,
            "path": str(dest),
            "title": entry.get("title") or "",
            "score": entry.get("score"),
            "status": "approved",
            "approved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "published": False,
        })
        _write_json(QUEUE_PATH, queue)
    return ""


def _handle_callback(token: str, owner_chat: str, cb: Dict[str, Any]) -> None:
    data = str(cb.get("data") or "")
    cb_id = cb.get("id")
    from_id = str(((cb.get("from") or {}).get("id")) or "")
    msg = cb.get("message") or {}
    if from_id != str(owner_chat):       # only the owner decides
        _call(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "Не твой завод 🙂"})
        return
    action, _, key = data.partition(":")
    with _LOCK:
        pending = _read_json(PENDING_PATH, {})
    entry = pending.get(key)
    if not entry:
        _call(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "Уже обработано"})
        return

    if action == "ap":
        err = _approve(entry)
        verdict = "✅ ОДОБРЕНО — клип в очереди на публикацию (output/approved/)" if not err \
                  else f"⚠️ Одобрено, но {err}"
        toast = "Одобрено ✅"
    else:
        verdict = "❌ ОТКЛОНЕНО"
        toast = "Отклонено ❌"

    with _LOCK:
        pending = _read_json(PENDING_PATH, {})
        pending.pop(key, None)
        _write_json(PENDING_PATH, pending)

    _call(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": toast})
    try:
        _call(token, "editMessageCaption", {
            "chat_id": entry.get("chat_id") or owner_chat,
            "message_id": entry.get("message_id") or msg.get("message_id"),
            "caption": (entry.get("caption") or "")[:900] + "\n\n" + verdict,
        })
    except Exception:
        pass


def _poll_once(token: str, owner_chat: str) -> None:
    offset = int(_read_json(OFFSET_PATH, {}).get("offset", 0))
    res = _call(token, "getUpdates", {
        "timeout": 25, "offset": offset, "allowed_updates": ["callback_query"],
    }, timeout=40)
    for upd in res.get("result") or []:
        offset = max(offset, int(upd.get("update_id", 0)) + 1)
        cb = upd.get("callback_query")
        if cb:
            try:
                _handle_callback(token, owner_chat, cb)
            except Exception as e:
                print("[tg] callback error:", repr(e)[:200])
    _write_json(OFFSET_PATH, {"offset": offset})


def start_review_poller(settings_reader) -> Optional[threading.Thread]:
    """Start the daemon getUpdates loop (called on API startup). settings_reader
    is a zero-arg callable returning fresh settings, so token edits apply live."""
    def _loop():
        misses = 0
        while True:
            try:
                settings = settings_reader() or {}
                token, chat = _tg_creds(settings)
                if not (token and chat):
                    time.sleep(15)
                    continue
                _poll_once(token, chat)
                misses = 0
            except Exception as e:
                misses += 1
                if misses in (1, 10):
                    print("[tg] poller error:", repr(e)[:160])
                time.sleep(min(60, 5 * misses))

    t = threading.Thread(target=_loop, name="tg-review-poller", daemon=True)
    t.start()
    print("[tg] review poller started (одобрение клипов через Telegram)")
    return t


__all__ = ["send_clip_for_review", "notify_clips_for_review",
           "start_review_poller", "review_enabled"]
