# -*- coding: utf-8 -*-
"""D-ID talking-avatar client for TREZZY (render_mode="avatar").

Turns a single portrait image + a text script into a talking-head MP4 by
calling the D-ID HTTP API. Pure stdlib (urllib) so it runs on Windows with no
extra pip installs.

Flow (matches D-ID docs):
  1. (optional) POST /images   — upload a local portrait, get a hosted source_url.
  2. POST /talks               — create the talk with source_url + script(text+voice).
  3. GET  /talks/{id}          — poll until status == "done", then read result_url.
  4. download result_url       — the final MP4.

Auth: header  Authorization: Basic <base64 of "<api_key>">  — D-ID accepts the
raw API key from Settings → API Keys, base64-encoded, as Basic credentials.

Cyrillic: send the script text as UTF-8; use a Microsoft Russian neural voice
(e.g. ru-RU-SvetlanaNeural) for a natural female RU voice.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import urllib.request
import urllib.error

API_BASE = "https://api.d-id.com"
DEFAULT_VOICE = "ru-RU-SvetlanaNeural"   # soft female Russian
DEFAULT_PROVIDER = "microsoft"


class DIDError(Exception):
    """Raised for any D-ID failure (auth, moderation, timeout, etc)."""


def _auth_header(api_key: str) -> str:
    token = base64.b64encode(api_key.encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _request(
    method: str,
    path: str,
    api_key: str,
    body: Optional[bytes] = None,
    content_type: Optional[str] = None,
    timeout: int = 60,
) -> Dict[str, Any]:
    headers = {
        "Authorization": _auth_header(api_key),
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",  # avoid Cloudflare 403 on default urllib UA
    }
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(API_BASE + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        # Friendlier message for the most common, expected failures.
        if e.code == 401:
            raise DIDError("D-ID auth failed (401). Check did_api_key in settings.")
        if e.code == 402:
            raise DIDError("D-ID out of credits (402). Top up or use a fresh trial key.")
        if e.code == 451:
            raise DIDError(
                "D-ID rejected the input by moderation (451). The avatar image or "
                "text was flagged. Use a clean, clothed head-and-shoulders portrait."
            )
        raise DIDError(f"D-ID HTTP {e.code}: {detail or e.reason}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise DIDError(f"D-ID unreachable: {e}")


def _multipart_image(image_path: Path) -> tuple[bytes, str]:
    """Build a minimal multipart/form-data body for POST /images."""
    boundary = "----trezzy" + uuid.uuid4().hex
    mime = mimetypes.guess_type(str(image_path))[0] or "image/png"
    data = image_path.read_bytes()
    pre = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{image_path.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8")
    post = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return pre + data + post, f"multipart/form-data; boundary={boundary}"


def upload_image(api_key: str, image_path: Path) -> str:
    """Upload a local portrait to D-ID; return the hosted image URL."""
    body, ctype = _multipart_image(Path(image_path))
    res = _request("POST", "/images", api_key, body=body, content_type=ctype, timeout=120)
    url = res.get("url")
    if not url:
        raise DIDError(f"D-ID image upload returned no url: {res}")
    return url


def _resolve_source_url(api_key: str, avatar_image: str, repo_root: Path) -> str:
    """avatar_image may be a public https URL or a filename under assets/avatar/."""
    if avatar_image.lower().startswith(("http://", "https://")):
        return avatar_image
    # treat as a local file: assets/avatar/<name>, else an absolute/relative path
    candidates = [
        repo_root / "assets" / "avatar" / avatar_image,
        repo_root / avatar_image,
        Path(avatar_image),
    ]
    for c in candidates:
        if c.exists() and c.is_file():
            return upload_image(api_key, c)
    raise DIDError(
        f"Avatar image not found: '{avatar_image}'. Put the file in assets/avatar/ "
        f"or set did_avatar_image to a public https URL."
    )


def create_talk(
    api_key: str,
    source_url: str,
    text: str,
    voice_id: str = DEFAULT_VOICE,
    voice_provider: str = DEFAULT_PROVIDER,
) -> str:
    """Create a talk; return its id."""
    payload: Dict[str, Any] = {
        "source_url": source_url,
        "script": {
            "type": "text",
            "input": text,
            "provider": {"type": voice_provider, "voice_id": voice_id},
        },
        "config": {"stitch": True},
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    res = _request("POST", "/talks", api_key, body=data,
                   content_type="application/json; charset=utf-8", timeout=60)
    talk_id = res.get("id")
    if not talk_id:
        raise DIDError(f"D-ID create talk returned no id: {res}")
    return talk_id


def poll_talk(api_key: str, talk_id: str, timeout_s: int = 240, interval_s: float = 3.0) -> str:
    """Poll /talks/{id} until done; return result_url."""
    deadline = time.time() + timeout_s
    last_status = "?"
    while time.time() < deadline:
        res = _request("GET", f"/talks/{talk_id}", api_key, timeout=30)
        last_status = res.get("status", "?")
        if last_status == "done":
            url = res.get("result_url")
            if not url:
                raise DIDError(f"D-ID talk done but no result_url: {res}")
            return url
        if last_status == "error":
            raise DIDError(f"D-ID talk failed: {res.get('error') or res}")
        time.sleep(interval_s)
    raise DIDError(f"D-ID talk timed out after {timeout_s}s (last status: {last_status}).")


def _download(url: str, dest: Path, timeout: int = 120) -> None:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        dest.write_bytes(r.read())


def render_avatar(
    plan: Dict[str, Any],
    job_dir: Path,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """Full avatar render: portrait + script -> final.mp4 in job_dir.

    Reads credentials/voice from `settings`. Mirrors output to output/latest
    and output/ like the local renderer. Raises DIDError on any failure.
    """
    api_key = (settings or {}).get("did_api_key") or os.getenv("DID_API_KEY") or ""
    if not api_key:
        raise DIDError(
            "No D-ID API key. Add did_api_key in Settings (get a free key at d-id.com)."
        )
    avatar_image = (settings or {}).get("did_avatar_image") or os.getenv("DID_AVATAR_IMAGE") or ""
    if not avatar_image:
        raise DIDError(
            "No avatar image set. Put a clothed head-and-shoulders portrait in "
            "assets/avatar/ and set did_avatar_image to its filename."
        )
    voice_id = (settings or {}).get("did_voice_id") or DEFAULT_VOICE
    voice_provider = (settings or {}).get("did_voice_provider") or DEFAULT_PROVIDER

    repo_root = Path(__file__).resolve().parents[2]
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    output_dir = repo_root / "output"
    latest_dir = output_dir / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)

    # The avatar speaks the script; keep it tight for short-form.
    spoken = (plan.get("voiceover_text") or plan.get("script") or plan.get("hook") or "").strip()
    if not spoken:
        raise DIDError("Plan has no text for the avatar to speak.")

    t0 = time.time()
    source_url = _resolve_source_url(api_key, avatar_image, repo_root)
    talk_id = create_talk(api_key, source_url, spoken, voice_id, voice_provider)
    result_url = poll_talk(api_key, talk_id)

    final_mp4 = job_dir / "final.mp4"
    _download(result_url, final_mp4)

    for dst in (latest_dir / "final.mp4", output_dir / "final.mp4"):
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(final_mp4.read_bytes())
        except Exception:
            pass

    return {
        "output_path": str(final_mp4),
        "package_dir": str(job_dir),
        "duration_seconds": round(time.time() - t0, 1),
        "renderer": "did_avatar",
        "talk_id": talk_id,
        "voice_id": voice_id,
    }


__all__ = ["render_avatar", "DIDError", "upload_image", "create_talk", "poll_talk"]
