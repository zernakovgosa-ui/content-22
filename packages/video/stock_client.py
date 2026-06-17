# -*- coding: utf-8 -*-
"""Pexels stock-video client (free official API) for render_mode="real".

Fetches REAL portrait/vertical footage so the assembled short looks human-shot
(not an AI text slide). Pure stdlib urllib — no extra installs.

Get a free key at https://www.pexels.com/api/ and put it in settings as
`pexels_api_key`. NEVER raises — returns [] on any failure so the renderer can
fall back gracefully.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import urllib.request
import urllib.error
import urllib.parse

PEXELS_SEARCH = "https://api.pexels.com/videos/search"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"  # avoid Cloudflare 403 on default urllib UA


def _request(url: str, api_key: str, timeout: int = 30) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"Authorization": api_key, "Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def search_portrait_clips(
    query: str, api_key: str, per_page: int = 6, min_duration: float = 2.0
) -> List[Dict[str, Any]]:
    """Return portrait clips [{link,width,height,duration}] for a search query.

    Picks, per video, the portrait video_file closest to 1080px wide (prefers HD).
    Returns [] on any failure (no key, network, no portrait results).
    """
    if not api_key or not (query or "").strip():
        return []
    params = urllib.parse.urlencode({
        "query": query.strip(),
        "orientation": "portrait",
        "size": "medium",
        "per_page": max(1, min(per_page, 80)),
    })
    try:
        data = _request(f"{PEXELS_SEARCH}?{params}", api_key)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        print(f"[stock] pexels HTTP {e.code} for '{query}': {detail or e.reason}")
        return []
    except Exception as e:
        print(f"[stock] pexels search failed for '{query}':", repr(e))
        return []

    out: List[Dict[str, Any]] = []
    for v in data.get("videos") or []:
        try:
            vw, vh = int(v.get("width", 0)), int(v.get("height", 0))
            dur = float(v.get("duration", 0) or 0)
            if vh <= vw or dur < min_duration:        # want vertical + usable length
                continue
            files = [
                f for f in (v.get("video_files") or [])
                if int(f.get("height", 0)) > int(f.get("width", 0)) and f.get("link")
            ]
            if not files:
                continue
            # Prefer ~1080px wide and HD quality (avoid 4K monsters).
            files.sort(key=lambda f: (
                abs(int(f.get("width", 0)) - 1080),
                0 if f.get("quality") == "hd" else 1,
            ))
            best = files[0]
            out.append({
                "link": best["link"],
                "width": int(best.get("width", 0)),
                "height": int(best.get("height", 0)),
                "duration": dur,
            })
        except Exception:
            continue
    return out


def download(url: str, dest: str | Path, timeout: int = 180) -> bool:
    """Download a clip to dest. Returns True on success (non-trivial file)."""
    dest = Path(dest)
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            dest.write_bytes(r.read())
        return dest.exists() and dest.stat().st_size > 1024
    except Exception as e:
        print("[stock] download failed:", repr(e))
        return False


__all__ = ["search_portrait_clips", "download"]
