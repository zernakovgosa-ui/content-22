# -*- coding: utf-8 -*-
"""Google Drive uploader — заливка записи статистики для buster-выплат.

Тот же Google-OAuth, что и YouTube (нужен доп. scope drive.file — доступ ТОЛЬКО
к файлам, что создаём мы, не ко всему диску). Грузим запись экрана статистики на
Drive аккаунта КАНАЛА → ставим «доступ по ссылке» → отдаём публичную ссылку для
формы выплаты. stdlib (urllib), стиль как clipper/publishers/youtube.py.
"""

from __future__ import annotations

import json
import mimetypes
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"


class DriveError(RuntimeError):
    pass


def _http_raw(req: urllib.request.Request, timeout: int = 60) -> Tuple[Optional[int], Dict[str, str], bytes]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
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


def upload_file(access_token: str, path: str | Path, name: str = "") -> str:
    """Залить файл на Drive (resumable, с резюмом в той же сессии — без дублей).
    Возвращает file_id."""
    path = Path(path)
    if not path.exists():
        raise DriveError(f"file not found: {path}")
    name = name or path.name
    size = path.stat().st_size
    ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    meta = json.dumps({"name": name}, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(UPLOAD_URL, data=meta, method="POST", headers={
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=utf-8",
        "X-Upload-Content-Type": ctype,
        "X-Upload-Content-Length": str(size),
        "User-Agent": UA,
    })
    status, headers, body = _http_raw(req)
    location = headers.get("Location") or headers.get("location")
    if status not in (200, 201) or not location:
        raise DriveError(f"resumable init failed: HTTP {status}: {body[:200]!r}")

    last = ""
    for attempt in range(1, 5):
        st: Optional[int] = None
        b2 = b""
        try:
            req2 = urllib.request.Request(location, data=path.read_bytes(), method="PUT", headers={
                "Content-Type": ctype,
                "Content-Range": f"bytes 0-{max(size - 1, 0)}/{size}",
                "User-Agent": UA,
            })
            st, _, b2 = _http_raw(req2, timeout=600)
        except Exception as e:
            last = str(e)
        if st in (200, 201):
            try:
                fid = json.loads(b2 or b"{}").get("id")
            except Exception:
                fid = None
            if fid:
                return fid
        # не подтвердилось — узнаём у сессии, не создался ли файл (без дубля)
        q = urllib.request.Request(location, data=b"", method="PUT",
                                   headers={"Content-Range": f"bytes */{size}", "User-Agent": UA})
        sq, _, bq = _http_raw(q, timeout=60)
        if sq in (200, 201):
            try:
                fid = json.loads(bq or b"{}").get("id")
                if fid:
                    return fid
            except Exception:
                pass
        last = last or f"HTTP {st}"
        time.sleep(3 * attempt)
    raise DriveError(f"upload failed after retries: {last}")


def make_public(access_token: str, file_id: str) -> None:
    """Доступ «любой по ссылке — читатель» (чтобы модер зашёл на запись)."""
    data = json.dumps({"role": "reader", "type": "anyone"}).encode("utf-8")
    req = urllib.request.Request(f"{FILES_URL}/{file_id}/permissions", data=data, method="POST",
        headers={"Authorization": f"Bearer {access_token}",
                 "Content-Type": "application/json; charset=utf-8", "User-Agent": UA})
    status, _, body = _http_raw(req)
    if status not in (200, 201):
        raise DriveError(f"permission failed: HTTP {status}: {body[:200]!r}")


def file_link(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"


__all__ = ["upload_file", "make_public", "file_link", "DriveError", "DRIVE_SCOPE"]
