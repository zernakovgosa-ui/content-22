# -*- coding: utf-8 -*-
"""HTTP client for trezzy-video-worker.

Calls the local worker at WORKER_HOST:WORKER_PORT/generate. Always sends
JSON as raw UTF-8 bytes so Cyrillic survives without Windows codepage games.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import urllib.request
import urllib.error


class WorkerUnavailable(Exception):
    pass


class VideoWorkerClient:
    def __init__(self, host: Optional[str] = None, port: Optional[int] = None, timeout: int = 300):
        self.host = host or os.getenv("WORKER_HOST", "127.0.0.1")
        self.port = int(port or os.getenv("WORKER_PORT", "8000"))
        self.timeout = timeout
        self.base_url = f"http://{self.host}:{self.port}"

    # --------------------------------------------------------------
    def health(self) -> Dict[str, Any]:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=5) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as e:
            raise WorkerUnavailable(str(e))

    # --------------------------------------------------------------
    def generate(
        self,
        hook: str,
        script: str,
        cta: str,
        title: str = "TREZZY",
        vibe_tags: Optional[List[str]] = None,
        caption: Optional[str] = None,
        hashtags: Optional[List[str]] = None,
        format: Optional[str] = None,
    ) -> Dict[str, Any]:
        body = {
            "hook":      hook,
            "title":     title,
            "script":    script,
            "vibe_tags": vibe_tags or [],
            "cta":       cta,
        }
        if caption:
            body["caption"] = caption
        if hashtags:
            body["hashtags"] = hashtags
        if format:
            body["format"] = format

        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/generate",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise WorkerUnavailable(f"Worker HTTP {e.code}: {detail or e.reason}")
        except (urllib.error.URLError, TimeoutError) as e:
            raise WorkerUnavailable(f"Worker unreachable: {e}")
