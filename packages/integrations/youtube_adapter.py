# -*- coding: utf-8 -*-
"""YouTube adapter — placeholder for YouTube Data API v3 / Shorts upload."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class YouTubeAdapter:
    name = "youtube"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or ""
        self.connected = bool(self.api_key)

    def status(self) -> str:
        return "needs_api_key" if not self.connected else "connected"

    def publish_short(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return {
            "platform":    self.name,
            "status":      "mock_queued" if not self.connected else "not_implemented",
            "video_path":  video_path,
            "title":       title,
            "description": description,
            "tags":        tags or [],
            "note":        "Нужен OAuth-клиент YouTube Data API v3 (videos.insert).",
        }

    def fetch_stats(self, video_id: Optional[str] = None) -> Dict[str, Any]:
        return {"platform": self.name, "source": "mock", "video_id": video_id}
