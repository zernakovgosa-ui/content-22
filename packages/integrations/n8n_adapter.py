# -*- coding: utf-8 -*-
"""n8n adapter — builds payloads for n8n HTTP Request nodes.

For local MVP we just persist a `n8n_payload.json` per job so an HTTP-trigger
workflow can read it and forward it downstream (Drive upload, Notion row, etc.).

If `N8N_WEBHOOK_URL` is set, `notify()` POSTs the payload to that webhook —
fire-and-forget; the API itself never blocks on n8n.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class N8nAdapter:
    name = "n8n"

    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = webhook_url or os.getenv("N8N_WEBHOOK_URL", "") or ""

    def build_payload(
        self,
        *,
        job_id: str,
        topic: str,
        format: str,
        platform: str,
        hook: str,
        script: str,
        caption: str,
        hashtags: List[str],
        cta: str,
        video_path: str,
        package_dir: str,
        duration_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        return {
            "schema":           "trezzy.job.v1",
            "job_id":           job_id,
            "topic":            topic,
            "format":           format,
            "platform":         platform,
            "hook":             hook,
            "script":           script,
            "caption":          caption,
            "hashtags":         hashtags,
            "cta":              cta,
            "video_path":       video_path,
            "package_dir":      package_dir,
            "duration_seconds": duration_seconds,
            "edit_notes_path":  f"{package_dir}/edit_notes.txt",
            "capcut_checklist": f"{package_dir}/capcut_checklist.md",
        }

    def notify(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.webhook_url:
            return {"status": "skipped", "reason": "N8N_WEBHOOK_URL is empty"}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                body = r.read().decode("utf-8", errors="replace")[:500]
                return {"status": "ok", "http_status": r.status, "body": body}
        except urllib.error.HTTPError as e:
            return {"status": "error", "http_status": e.code, "detail": str(e)}
        except (urllib.error.URLError, TimeoutError) as e:
            return {"status": "error", "detail": str(e)}
