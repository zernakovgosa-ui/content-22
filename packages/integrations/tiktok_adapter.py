# -*- coding: utf-8 -*-
"""TikTok adapter — placeholder for TikTok Content Posting API."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class TikTokAdapter:
    name = "tiktok"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or ""
        self.connected = bool(self.api_key)

    def status(self) -> str:
        return "needs_api_key" if not self.connected else "connected"

    def publish(
        self,
        video_path: str,
        caption: str,
        hashtags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return {
            "platform":   self.name,
            "status":     "mock_queued" if not self.connected else "not_implemented",
            "video_path": video_path,
            "caption":    caption,
            "hashtags":   hashtags or [],
            "note":       "Нужна авторизация в TikTok for Developers + Content Posting API.",
        }

    def fetch_stats(self, video_id: Optional[str] = None) -> Dict[str, Any]:
        return {"platform": self.name, "source": "mock", "video_id": video_id}
