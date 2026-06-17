# -*- coding: utf-8 -*-
"""YouTube Data API v3 publisher — official auto-posting for Shorts.

Pure stdlib (urllib), same style as the rest of the factory. Flow:

  1. Owner creates ONE OAuth client of type "Web application" in Google Cloud
     Console (APIs & Services → Credentials) and enables "YouTube Data API v3".
     The same client_id/client_secret is reused for every channel. CRITICAL:
     under "Authorized redirect URIs" add EXACTLY
     http://localhost:8002/auth/yt/callback — otherwise Google returns
     "Error 400: redirect_uri_mismatch". A "Desktop app" client can't register
     this path, so it must be "Web application".
  2. Per account: build_auth_url() → owner logs into THAT channel's Google
     account → Google redirects to http://localhost:8002/auth/yt/callback →
     exchange_code() → we store the refresh_token.
  3. At post time: refresh_access_token() → upload_video() (resumable,
     "#Shorts" in the title) → returns video_id.
  4. fetch_stats() pulls views/likes for our uploaded videos so the 🚀/⚠
     notifications work with zero manual input.

Note: in Google's consent screen set the app to "In production" — tokens of
apps left "In testing" expire after 7 days.
"""

from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"   # Cloudflare/Google edge friendliness
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = ("https://www.googleapis.com/upload/youtube/v3/videos"
              "?uploadType=resumable&part=snippet,status")
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"


class YouTubeError(RuntimeError):
    pass


def _http(req: urllib.request.Request, timeout: int = 60) -> Tuple[int, Dict[str, str], bytes]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        raise YouTubeError(f"HTTP {e.code}: {body.decode('utf-8', 'replace')[:400]}") from None
    except Exception as e:
        raise YouTubeError(f"network error: {e}") from None


def build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Consent URL for an installed-app loopback flow (offline → refresh_token)."""
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",          # force refresh_token even on re-auth
        "state": state,
    })
    return f"{AUTH_URL}?{params}"


def _token_request(payload: Dict[str, str]) -> Dict[str, Any]:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA},
    )
    _, _, body = _http(req)
    return json.loads(body)


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> Dict[str, Any]:
    """Auth code → {access_token, refresh_token, ...}."""
    res = _token_request({
        "client_id": client_id, "client_secret": client_secret,
        "code": code, "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })
    if not res.get("refresh_token"):
        raise YouTubeError(f"no refresh_token in response: {str(res)[:200]}")
    return res


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    res = _token_request({
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token, "grant_type": "refresh_token",
    })
    token = res.get("access_token")
    if not token:
        raise YouTubeError(f"no access_token: {str(res)[:200]}")
    return token


def upload_video(access_token: str, path: str | Path, title: str,
                 description: str = "", tags: Optional[List[str]] = None,
                 privacy: str = "public") -> str:
    """Resumable upload → returns the new video_id. '#Shorts' makes it a Short."""
    path = Path(path)
    if not path.exists():
        raise YouTubeError(f"file not found: {path}")
    title = (title or "Shorts").strip()[:95]
    if "#shorts" not in title.lower():
        title = f"{title} #Shorts"

    meta = {
        "snippet": {
            "title": title,
            "description": (description or "")[:4500],
            "tags": (tags or [])[:15],
            "categoryId": "24",          # Entertainment
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    body = json.dumps(meta, ensure_ascii=False).encode("utf-8")
    size = path.stat().st_size
    ctype = mimetypes.guess_type(str(path))[0] or "video/mp4"

    # Step 1: initiate the resumable session.
    req = urllib.request.Request(UPLOAD_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=utf-8",
        "X-Upload-Content-Type": ctype,
        "X-Upload-Content-Length": str(size),
        "User-Agent": UA,
    })
    status, headers, _ = _http(req)
    location = headers.get("Location") or headers.get("location")
    if status not in (200, 201) or not location:
        raise YouTubeError(f"resumable init failed: HTTP {status}, no Location")

    # Step 2: PUT the whole file (clips are tens of MB — one shot is fine).
    req2 = urllib.request.Request(location, data=path.read_bytes(), method="PUT", headers={
        "Content-Type": ctype,
        "User-Agent": UA,
    })
    _, _, body2 = _http(req2, timeout=600)
    res = json.loads(body2)
    vid = res.get("id")
    if not vid:
        raise YouTubeError(f"upload finished but no video id: {str(res)[:200]}")
    return vid


def fetch_stats(access_token: str, video_ids: List[str]) -> Dict[str, Dict[str, int]]:
    """{video_id: {views, likes}} for up to 50 ids per call. Best-effort."""
    out: Dict[str, Dict[str, int]] = {}
    ids = [v for v in video_ids if v]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        params = urllib.parse.urlencode({"part": "statistics", "id": ",".join(chunk)})
        req = urllib.request.Request(f"{VIDEOS_URL}?{params}", headers={
            "Authorization": f"Bearer {access_token}", "User-Agent": UA,
        })
        try:
            _, _, body = _http(req)
            for item in json.loads(body).get("items") or []:
                st = item.get("statistics") or {}
                out[item["id"]] = {
                    "views": int(st.get("viewCount", 0) or 0),
                    "likes": int(st.get("likeCount", 0) or 0),
                }
        except Exception as e:
            print("[yt] stats fetch failed:", str(e)[:160])
    return out


__all__ = ["build_auth_url", "exchange_code", "refresh_access_token",
           "upload_video", "fetch_stats", "YouTubeError", "SCOPES"]
