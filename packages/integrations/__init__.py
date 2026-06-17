"""Safe placeholder adapters for external platforms.

None of these implement scraping or unofficial login. They are token/API-key
shaped so real integrations can be slotted in without touching the API.
"""

from .instagram_adapter import InstagramAdapter
from .tiktok_adapter import TikTokAdapter
from .youtube_adapter import YouTubeAdapter
from .capcut_adapter import CapCutAdapter
from .n8n_adapter import N8nAdapter

__all__ = [
    "InstagramAdapter",
    "TikTokAdapter",
    "YouTubeAdapter",
    "CapCutAdapter",
    "N8nAdapter",
]
