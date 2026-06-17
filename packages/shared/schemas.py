# -*- coding: utf-8 -*-
"""Shared request/response schemas for the API."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class PlanRequest(BaseModel):
    topic: str
    product_name: Optional[str] = None
    target_audience: Optional[str] = None
    platform: str = "instagram"  # instagram | tiktok | youtube | all
    format: str = "single_review"
    style: str = "premium luxury perfume"
    seed: Optional[int] = None


class GenerateFromPlanRequest(BaseModel):
    topic: str
    format: str = "single_review"
    product_name: Optional[str] = None
    target_audience: Optional[str] = None
    platform: str = "instagram"
    style: str = "premium luxury perfume"
    quantity: int = Field(1, ge=1, le=5)
    seed: Optional[int] = None
    render_mode: str = "fast"  # fast = local renderer; avatar = D-ID; clip = cut a long video; worker = HTTP video-worker
    # ── Clip mode (render_mode="clip"): repurpose a long LOCAL video into shorts ──
    source_video: Optional[str] = None        # filename under assets/source/ (or an absolute path)
    clip_count: int = Field(0, ge=0, le=30)   # 0 = auto (2 clips per 10 min of source)


class AccountIn(BaseModel):
    platform: str  # instagram | tiktok | youtube
    handle: str
    display_name: Optional[str] = None
    status: str = "needs_api_key"  # mock_connected | needs_api_key | disabled
    api_key: str = ""
    notes: Optional[str] = ""


class SettingsIn(BaseModel):
    default_style: Optional[str] = None
    default_cta: Optional[str] = None
    default_brand: Optional[str] = None
    default_platform: Optional[str] = None
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    elevenlabs_api_key: Optional[str] = None
    n8n_webhook_url: Optional[str] = None
    capcut_workflow_notes: Optional[str] = None
    # ── D-ID talking-avatar (render_mode="avatar") ──
    did_api_key: Optional[str] = None            # from d-id.com Settings → API key
    did_avatar_image: Optional[str] = None       # filename under assets/avatar/ OR a public https URL
    did_voice_id: Optional[str] = None           # e.g. ru-RU-SvetlanaNeural
    did_voice_provider: Optional[str] = None     # "microsoft" (default) | "amazon" | "elevenlabs"
    # ── Clip mode (render_mode="clip") ──
    groq_api_key: Optional[str] = None           # free tier at console.groq.com — Whisper STT + free LLM
    clip_transcriber: Optional[str] = None       # "groq" (default) | "openai" | "whispercpp"
    clip_face_tracking: Optional[bool] = None    # crop 9:16 toward the detected face (needs opencv)
    clip_burn_captions: Optional[bool] = None    # burn subtitles (from the audio) into each clip
    # ── Realistic mode (render_mode="real"): real Pexels stock footage ──
    pexels_api_key: Optional[str] = None         # free key at pexels.com/api — real stock clips
    stock_clip_count: Optional[int] = None       # how many real clips to stitch (default 5)
    stock_seconds_per_clip: Optional[float] = None  # seconds shown per clip (default 3.5)
    # ── Render quality (clip + real modes) ──
    render_quality: Optional[str] = None         # "max" (default: slow, best) | "fast"
    clip_framing: Optional[str] = None           # "wide" (default: subject smaller, blur bars) | "fill"
    # ── Telegram review bot (official Bot API; rule #3 — no password posting) ──
    telegram_bot_token: Optional[str] = None     # from @BotFather
    telegram_chat_id: Optional[str] = None       # owner's chat id (bot must be /start-ed)


class JobStatus(BaseModel):
    job_id: str
    status: str  # planned | rendering | ready | failed
    created_at: str
    finished_at: Optional[str] = None
    format: str
    platform: str
    topic: str
    output_path: Optional[str] = None
    package_dir: Optional[str] = None
    duration_seconds: Optional[float] = None
    error: Optional[str] = None
    qc_score: Optional[float] = None
    qc_ready: Optional[bool] = None
    hashtags: List[str] = Field(default_factory=list)
