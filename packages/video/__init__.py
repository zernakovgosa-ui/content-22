"""Thin wrapper around the trezzy-video-worker HTTP service, plus a
local in-process fast renderer and a D-ID talking-avatar renderer."""

from .worker_client import VideoWorkerClient, WorkerUnavailable
from .local_renderer import render_fast
from .did_client import render_avatar, DIDError
from .clip_renderer import render_clips, video_duration
from .transcribe import transcribe
from .stock_renderer import render_stock

__all__ = [
    "VideoWorkerClient",
    "WorkerUnavailable",
    "render_fast",
    "render_avatar",
    "DIDError",
    "render_clips",
    "video_duration",
    "transcribe",
    "render_stock",
]
