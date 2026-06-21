# -*- coding: utf-8 -*-
"""YouTube Data API v3 publisher — official auto-posting for Shorts.

Pure stdlib (urllib), same style as the rest of the factory. Flow:

  1. Owner creates ONE OAuth client of type "Web application" in Google Cloud
     Console (APIs & Services → Credentials) and enables "YouTube Data API v3".
     The same client_id/client_secret is reused for every channel. The redirect
     URI is built dynamically by the server (clipper.server._yt_redirect): for a
     local run it is http://localhost:8002/auth/yt/callback, for a server deploy
     with public_base_url it is https://<домен>/auth/yt/callback. Add BOTH
     variants under "Authorized redirect URIs" — otherwise Google returns
     "Error 400: redirect_uri_mismatch". A "Desktop app" client can't register
     these paths, so it must be "Web application".
  2. Per account: build_auth_url() → owner logs into THAT channel's Google
     account → Google redirects to the callback → exchange_code() → we store the
     refresh_token.
  3. At post time: refresh_access_token() → upload_video() (resumable,
     "#Shorts" in the title) → returns video_id.
  4. fetch_stats() pulls views/likes for our uploaded videos so the 🚀/⚠
     notifications work with zero manual input.

Note: in Google's consent screen set the app to "In production" — tokens of
apps left "In testing" expire after 7 days. Google then returns invalid_grant,
which we surface as YouTubeAuthError so the owner reconnects the channel instead
of the poster retrying a dead token for hours.
"""

from __future__ import annotations

import json
import mimetypes
import time
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
TITLE_LIMIT = 100   # YouTube hard limit; over → API 400 "Invalid title"


class YouTubeError(RuntimeError):
    pass


class YouTubeAuthError(YouTubeError):
    """refresh_token отозван/протух (invalid_grant) — нужен повторный коннект
    канала. Ретраить бессмысленно, поэтому это отдельный класс ошибки."""
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Resumable uploads answer with HTTP 308 (Resume Incomplete). urllib would
    # try to "follow" it as a redirect — we must see the raw 308 instead.
    def redirect_request(self, *a, **k):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _http_raw(req: urllib.request.Request, timeout: int = 60) -> Tuple[Optional[int], Dict[str, str], bytes]:
    """Like _http but returns the REAL status even for 308/4xx (never raises on
    HTTP status); status=None on a network-level error. Used by the resumable
    status query, which relies on distinguishing 308 (incomplete) from 200/201
    (video already created)."""
    try:
        with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        return e.code, dict(e.headers or {}), body
    except Exception:
        return None, {}, b""


def build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Consent URL (Web-app flow, offline → refresh_token). redirect_uri is the
    one from clipper.server._yt_redirect() and MUST be registered in Console."""
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
    try:
        res = _token_request({
            "client_id": client_id, "client_secret": client_secret,
            "refresh_token": refresh_token, "grant_type": "refresh_token",
        })
    except YouTubeError as e:
        # 400 invalid_grant = токен отозван/протух (сменён пароль, снят доступ,
        # приложение «In testing» >7 дней). Это НЕ сетевой сбой — отдельная ошибка.
        if "invalid_grant" in str(e).lower():
            raise YouTubeAuthError("refresh_token отозван/протух — переподключи канал") from None
        raise
    token = res.get("access_token")
    if not token:
        if "invalid_grant" in str(res).lower():
            raise YouTubeAuthError("refresh_token отозван/протух — переподключи канал")
        raise YouTubeError(f"no access_token: {str(res)[:200]}")
    return token


def _resumable_status(location: str, size: int) -> Optional[str]:
    """Спросить у сессии resumable-загрузки её состояние, НЕ заливая файл заново.
    Возвращает video_id, если видео уже создано (200/201) — это и есть защита от
    дубля: если ответ на PUT потерялся, мы узнаём id, а не льём второй раз. None,
    если сессия неполная (308) или сеть моргнула."""
    req = urllib.request.Request(location, data=b"", method="PUT", headers={
        "Content-Range": f"bytes */{size}", "User-Agent": UA,
    })
    status, _, body = _http_raw(req, timeout=60)
    if status in (200, 201):
        try:
            return json.loads(body or b"{}").get("id")
        except Exception:
            return None
    return None


def upload_video(access_token: str, path: str | Path, title: str,
                 description: str = "", tags: Optional[List[str]] = None,
                 privacy: str = "public") -> str:
    """Resumable upload → returns the new video_id. '#Shorts' makes it a Short.

    Резюмит в рамках ОДНОЙ сессии: при обрыве сети не открывает новую сессию
    (что плодило бы дубли роликов), а спрашивает у текущей, создалось ли видео,
    и только при реальной недокачке досылает файл. Так потерянный ответ при
    нестабильной сети не превращается в два одинаковых Shorts."""
    path = Path(path)
    if not path.exists():
        raise YouTubeError(f"file not found: {path}")
    if privacy not in ("public", "unlisted", "private"):
        privacy = "public"
    title = (title or "Shorts").strip()
    tag = "" if "#shorts" in title.lower() else " #Shorts"
    title = title[:TITLE_LIMIT - len(tag)].rstrip() + tag   # гарантированно ≤100

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

    # Step 1: initiate the resumable session ONCE. Re-initiating is exactly what
    # would create duplicate videos, so all retrying happens inside this session.
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

    # Step 2: upload to the SAME session, with resume + dup-safe status checks.
    last_err = ""
    for attempt in range(1, 5):
        status2: Optional[int] = None
        body2 = b""
        try:
            req2 = urllib.request.Request(location, data=path.read_bytes(), method="PUT", headers={
                "Content-Type": ctype,
                "Content-Range": f"bytes 0-{max(size - 1, 0)}/{size}",
                "User-Agent": UA,
            })
            status2, _, body2 = _http_raw(req2, timeout=600)
        except Exception as e:
            last_err = str(e)
        if status2 in (200, 201):
            try:
                vid = json.loads(body2 or b"{}").get("id")
            except Exception:
                vid = None
            if vid:
                return vid
        # Ответ не подтвердил успех — но не создалось ли видео несмотря на обрыв?
        done = _resumable_status(location, size)
        if done:
            return done
        last_err = last_err or f"HTTP {status2}"
        time.sleep(4 * attempt)
    raise YouTubeError(f"upload failed after resume attempts: {last_err}")


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
           "upload_video", "fetch_stats", "YouTubeError", "YouTubeAuthError", "SCOPES"]
