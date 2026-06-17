# -*- coding: utf-8 -*-
"""Instagram adapter — placeholder.

Real implementation should use Instagram Graph API with a Long-Lived User Token.
Never implement unofficial login or scraping.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class InstagramAdapter:
    name = "instagram"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or ""
        self.connected = bool(self.api_key)

    def status(self) -> str:
        return "mock_connected" if not self.connected else "connected"

    def list_accounts(self) -> List[Dict[str, Any]]:
        # In a real build this would call /me/accounts. For MVP, return [].
        return []

    def publish_reel(
        self,
        video_path: str,
        caption: str,
        hashtags: Optional[List[str]] = None,
        cover_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """No-op stub.

        Returns the payload that would be sent so callers can persist / inspect.
        """
        return {
            "platform":   self.name,
            "status":     "mock_queued" if not self.connected else "not_implemented",
            "video_path": video_path,
            "caption":    caption,
            "hashtags":   hashtags or [],
            "cover_path": cover_path,
            "note":       "Подключи Instagram Graph API token в Settings, чтобы реально публиковать.",
        }

    def fetch_stats(self, media_id: Optional[str] = None) -> Dict[str, Any]:
        return {"platform": self.name, "source": "mock", "media_id": media_id}
