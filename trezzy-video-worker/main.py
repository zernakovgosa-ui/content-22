# -*- coding: utf-8 -*-
"""
TREZZY Content Factory - local video worker (v0.4).

Premium TikTok / Reels perfume short renderer:
  - Asset-aware: uses files from assets/{backgrounds,perfume,overlays,music,fonts}
    when present; otherwise falls back to an animated dark-gold gradient
    with drifting gold particles, vignette and grain.
  - Scene library: HOOK -> SPOKESPERSON (UGC placeholder) -> PRODUCT VIBE
    -> PROOF / FEELING -> CTA. Scene set + pacing varies by format.
  - Tight text chunking (max_words_per_screen), mobile-safe margins,
    serif gold accent line, no huge centred paragraphs.
  - Russian text supported through Windows system fonts (Georgia/Arial)
    or any TTF dropped into assets/fonts/.

Public surface preserved:
  POST /generate            -> hook/script/cta + optional title/vibe_tags
  POST /plan                -> content_brain plan
  POST /generate-from-plan  -> plan + render
Output files preserved:
  output/latest/final.mp4 + script.txt, caption.txt, hashtags.txt,
  edit_notes.txt, capcut_checklist.md, request.json
  output/final.mp4 (compatibility copy)
"""

from __future__ import annotations  # defer annotations so optional moviepy/numpy types don't eval at import

import json as json_lib
import os
import random
import re
import shutil
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from content_brain import SUPPORTED_FORMATS, make_plan

# moviepy 1.0.3 still references Image.ANTIALIAS, which Pillow 10 removed.
if not hasattr(Image, "ANTIALIAS"):
    Image.ANTIALIAS = Image.LANCZOS  # type: ignore[attr-defined]

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    from moviepy.editor import (
        AudioFileClip,
        ColorClip,
        CompositeVideoClip,
        ImageClip,
        VideoClip,
        VideoFileClip,
        concatenate_videoclips,
        afx,
    )
    MOVIEPY_AVAILABLE = True
except Exception as _moviepy_err:  # heavy 'worker' render is OPTIONAL (see requirements-worker.txt)
    AudioFileClip = ColorClip = CompositeVideoClip = ImageClip = None  # type: ignore
    VideoClip = VideoFileClip = concatenate_videoclips = afx = None     # type: ignore
    MOVIEPY_AVAILABLE = False
    print("[worker] moviepy not installed — heavy 'worker' render disabled. "
          "fast/avatar/clip render in the API are unaffected. Detail:", repr(_moviepy_err))

load_dotenv()

ROOT = Path(__file__).parent.resolve()
OUTPUT_DIR = ROOT / "output"
ASSETS_DIR = ROOT / "assets"
LATEST_DIR = OUTPUT_DIR / "latest"
ASSET_SUBDIRS = ("backgrounds", "perfume", "overlays", "music", "fonts")
RENDER_CONFIG_PATH = ROOT / "render_config.json"


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
def _bootstrap_dirs() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    LATEST_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(exist_ok=True)
    for sub in ASSET_SUBDIRS:
        d = ASSETS_DIR / sub
        d.mkdir(parents=True, exist_ok=True)
        keep = d / ".gitkeep"
        if not keep.exists():
            keep.touch()


_bootstrap_dirs()


# ---------------------------------------------------------------------------
# Render config
# ---------------------------------------------------------------------------
_DEFAULT_RENDER_CONFIG: Dict[str, Any] = {
    "style": "premium_luxury",
    "use_assets": True,
    "fallback_animated_background": True,
    "max_words_per_screen": 6,
    "enable_particles": True,
    "enable_vignette": True,
    "enable_music": True,
    "music_volume": 0.35,
    "scene_durations": {
        "hook":          2.4,
        "spokesperson":  2.2,
        "product_vibe":  3.4,
        "proof_feeling": 2.8,
        "cta":           2.6,
        "ugc_chunk":     1.4,
        "premium_main":  5.2,
    },
    "fade_seconds":     0.30,
    "ugc_fade_seconds": 0.12,
    "safe_margin_px":   96,
    "fps":              30,
}


def _load_render_config() -> Dict[str, Any]:
    cfg = dict(_DEFAULT_RENDER_CONFIG)
    cfg["scene_durations"] = dict(_DEFAULT_RENDER_CONFIG["scene_durations"])
    if not RENDER_CONFIG_PATH.exists():
        return cfg
    try:
        loaded = json_lib.loads(RENDER_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[config] failed to read {RENDER_CONFIG_PATH.name}: {e}; using defaults", flush=True)
        return cfg
    for k, v in loaded.items():
        if k == "scene_durations" and isinstance(v, dict):
            cfg["scene_durations"].update(v)
        else:
            cfg[k] = v
    return cfg


CFG = _load_render_config()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WIDTH = 1080
HEIGHT = 1920
FPS = int(CFG.get("fps", 30))
SAFE_MARGIN = int(CFG.get("safe_margin_px", 96))

BG_COLOR  = (8, 6, 10)
GOLD      = (198, 162, 102)
GOLD_DIM  = (130, 100, 60)
GOLD_SOFT = (240, 210, 150)
WHITE     = (248, 244, 235)
SOFT      = (180, 170, 160)
GLOW_TINT = (58, 36, 20)

DEFAULT_VIBE_TAGS = ["элегантно", "дорого", "уверенно"]
DEFAULT_HOOK = "Премиум аромат"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg"}
FONT_EXTS  = {".ttf", ".otf"}

app = FastAPI(title="TREZZY Video Worker", version="0.4.0")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class GenerateRequest(BaseModel):
    """Content factory request payload."""
    hook: Optional[str] = Field(None, description="Big emotional hook for the opening scene")
    title: Optional[str] = Field(None, description="Brand mark (new) or headline (old)")
    script: str = Field(..., description="Body text shown as cinematic chunks")
    vibe_tags: Optional[List[str]] = Field(None, description="Up to 3 short vibe pills")
    cta: str = Field(..., description="Call to action shown on the final scene")
    caption: Optional[str] = Field(None, description="Social caption; generated if missing")
    hashtags: Optional[List[str]] = Field(None, description="Hashtags; generated if missing")
    format: Optional[str] = Field(None, description="Optional format hint (e.g. ai_ugc_ad)")


_HASHTAG_BASE = [
    "#trezzy", "#parfum", "#perfume", "#fragrance",
    "#luxury", "#niche", "#парфюмерия",
]


def _slugify_hashtag(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.startswith("#"):
        return "#" + re.sub(r"\s+", "", value[1:]).lower()
    return "#" + re.sub(r"\s+", "", value).lower()


def _default_caption(hook: str, script: str, cta: str) -> str:
    return f"{hook}\n\n{script}\n\n{cta}".strip()


def _default_hashtags(brand: str, vibe_tags: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in [f"#{brand.lower()}", *_HASHTAG_BASE, *(_slugify_hashtag(t) for t in vibe_tags)]:
        if raw and raw not in seen:
            seen.add(raw)
            out.append(raw)
    return out[:15]


def _normalize_request(req: GenerateRequest) -> dict:
    if req.hook and req.hook.strip():
        hook = req.hook.strip()
        brand = (req.title or "TREZZY").strip()
    else:
        hook = (req.title or DEFAULT_HOOK).strip()
        brand = "TREZZY"

    raw_tags = req.vibe_tags or DEFAULT_VIBE_TAGS
    tags = [t.strip() for t in raw_tags if t and t.strip()][:3]
    if not tags:
        tags = list(DEFAULT_VIBE_TAGS)

    cta = req.cta.strip()
    script = req.script.strip()

    if req.caption and req.caption.strip():
        caption = req.caption.strip()
    else:
        caption = _default_caption(hook, script, cta)

    if req.hashtags:
        hashtags: List[str] = []
        seen = set()
        for h in req.hashtags:
            slug = _slugify_hashtag(h)
            if slug and slug not in seen:
                seen.add(slug)
                hashtags.append(slug)
        if not hashtags:
            hashtags = _default_hashtags(brand, tags)
    else:
        hashtags = _default_hashtags(brand, tags)

    return {
        "hook":      hook,
        "brand":     brand,
        "script":    script,
        "vibe_tags": tags,
        "cta":       cta,
        "caption":   caption,
        "hashtags":  hashtags,
        "format":    (req.format or "").strip().lower() or None,
    }


# ---------------------------------------------------------------------------
# Asset discovery
# ---------------------------------------------------------------------------
_ASSET_RNG = random.Random()


def _list_assets(subdir: str, exts: set) -> List[Path]:
    folder = ASSETS_DIR / subdir
    if not folder.exists():
        return []
    out: List[Path] = []
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in exts:
            out.append(p)
    out.sort()
    return out


def _pick_asset(subdir: str, exts: set) -> Optional[Path]:
    items = _list_assets(subdir, exts)
    if not items:
        return None
    return _ASSET_RNG.choice(items)


def _pick_font_file() -> Optional[Path]:
    return _pick_asset("fonts", FONT_EXTS)


def _pick_background() -> Optional[Tuple[str, Path]]:
    """Return ('video', path) or ('image', path) or None."""
    videos = _list_assets("backgrounds", VIDEO_EXTS)
    if videos:
        return ("video", _ASSET_RNG.choice(videos))
    images = _list_assets("backgrounds", IMAGE_EXTS)
    if images:
        return ("image", _ASSET_RNG.choice(images))
    return None


def _pick_perfume_image() -> Optional[Path]:
    return _pick_asset("perfume", IMAGE_EXTS)


def _pick_overlay() -> Optional[Tuple[str, Path]]:
    videos = _list_assets("overlays", VIDEO_EXTS)
    if videos:
        return ("video", _ASSET_RNG.choice(videos))
    images = _list_assets("overlays", IMAGE_EXTS)
    if images:
        return ("image", _ASSET_RNG.choice(images))
    return None


def _pick_music() -> Optional[Path]:
    return _pick_asset("music", AUDIO_EXTS)


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
_FONT_CANDIDATES_BOLD = [
    r"C:\Windows\Fonts\georgiab.ttf",
    r"C:\Windows\Fonts\timesbd.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",
]
_FONT_CANDIDATES_REGULAR = [
    r"C:\Windows\Fonts\georgia.ttf",
    r"C:\Windows\Fonts\times.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
]
_FONT_CANDIDATES_ITALIC = [
    r"C:\Windows\Fonts\georgiai.ttf",
    r"C:\Windows\Fonts\timesi.ttf",
    r"C:\Windows\Fonts\ariali.ttf",
    r"C:\Windows\Fonts\segoeuii.ttf",
]


def _font_has_glyphs(font_path: str, sample: str) -> bool:
    try:
        tt = ImageFont.truetype(font_path, 24)
        face = tt.font  # type: ignore[attr-defined]
        for ch in sample:
            if ch.isspace():
                continue
            if face.getsize(ch)[0] == 0:
                return False
        return True
    except Exception:
        return True


def _load_font(
    size: int,
    bold: bool = False,
    italic: bool = False,
    sample: str = "",
) -> ImageFont.FreeTypeFont:
    # 1) Try a custom font from assets/fonts (only if it has the glyphs we need).
    custom = _pick_font_file()
    if custom is not None:
        path = str(custom)
        try:
            if not sample or _font_has_glyphs(path, sample):
                return ImageFont.truetype(path, size)
        except OSError:
            pass

    # 2) Fall back to Windows system fonts.
    if italic:
        primary = _FONT_CANDIDATES_ITALIC
    elif bold:
        primary = _FONT_CANDIDATES_BOLD
    else:
        primary = _FONT_CANDIDATES_REGULAR
    candidates = list(primary) + list(_FONT_CANDIDATES_REGULAR)

    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            if sample and not _font_has_glyphs(path, sample):
                continue
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Background generation (static still + animated VideoClip fallback)
# ---------------------------------------------------------------------------
_BG_CACHE: Dict[Tuple[float, float], Image.Image] = {}


def _make_still_background(glow_cx: float = 0.5, glow_cy: float = 0.42) -> Image.Image:
    """Dark base + warm radial glow + radial vignette + film grain."""
    key = (round(glow_cx, 2), round(glow_cy, 2))
    if key in _BG_CACHE:
        return _BG_CACHE[key].copy()

    arr = np.full((HEIGHT, WIDTH, 3), BG_COLOR, dtype=np.int32)
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    gx, gy = int(WIDTH * glow_cx), int(HEIGHT * glow_cy)
    dist_glow = np.sqrt((xx - gx) ** 2 + (yy - gy) ** 2).astype(np.float32)
    max_dist = float(np.sqrt(WIDTH ** 2 + HEIGHT ** 2))

    glow = np.exp(-((dist_glow / (max_dist * 0.28)) ** 2))
    for c in range(3):
        arr[..., c] = arr[..., c] + (glow * GLOW_TINT[c]).astype(np.int32)

    if CFG.get("enable_vignette", True):
        ccx, ccy = WIDTH // 2, HEIGHT // 2
        dist_v = np.sqrt((xx - ccx) ** 2 + (yy - ccy) ** 2).astype(np.float32)
        vignette = 1.0 - np.exp(-((dist_v / (max_dist * 0.62)) ** 2))
        vignette = np.clip(vignette, 0.0, 0.7)
        for c in range(3):
            arr[..., c] = (arr[..., c] * (1.0 - vignette)).astype(np.int32)

    rng = np.random.default_rng(2026)
    grain = rng.integers(-5, 6, size=(HEIGHT, WIDTH), dtype=np.int32)
    for c in range(3):
        arr[..., c] = arr[..., c] + grain

    arr = np.clip(arr, 0, 255).astype(np.uint8)
    img = Image.fromarray(arr, mode="RGB").filter(ImageFilter.GaussianBlur(radius=1.2))
    _BG_CACHE[key] = img
    return img.copy()


# Pre-generated particle seed table. Each row = (x0, y0, drift_x, drift_y,
# radius, alpha) at t=0; positions wrap vertically with time.
_PARTICLE_STATE: Optional[np.ndarray] = None


def _particle_state(n: int = 38) -> np.ndarray:
    global _PARTICLE_STATE
    if _PARTICLE_STATE is not None and _PARTICLE_STATE.shape[0] == n:
        return _PARTICLE_STATE
    rng = np.random.default_rng(73)
    xs = rng.uniform(0, WIDTH, size=n)
    ys = rng.uniform(0, HEIGHT, size=n)
    dx = rng.uniform(-12, 12, size=n)                 # px per second sideways
    dy = rng.uniform(-55, -22, size=n)                # px per second upward
    radii = rng.uniform(1.4, 3.6, size=n)
    alphas = rng.uniform(0.35, 0.95, size=n)
    _PARTICLE_STATE = np.stack([xs, ys, dx, dy, radii, alphas], axis=1)
    return _PARTICLE_STATE


def _animated_background_clip(
    duration: float,
    glow_cx: float = 0.5,
    glow_cy: float = 0.42,
    drift: float = 1.0,
) -> VideoClip:
    """Return a VideoClip with subtle drifting gold particles over the still bg.

    Particles only render if config enables them, otherwise we return a still
    ImageClip (no per-frame work) which is faster.
    """
    still = _make_still_background(glow_cx=glow_cx, glow_cy=glow_cy)
    still_arr = np.array(still.convert("RGB"))

    if not CFG.get("enable_particles", True):
        return ImageClip(still_arr).set_duration(duration)

    base_int = still_arr.astype(np.int16)
    state = _particle_state()
    n = state.shape[0]

    def make_frame(t: float) -> np.ndarray:
        frame = base_int.copy()
        # Position at time t (with wrap on Y so particles cycle).
        xs = (state[:, 0] + state[:, 2] * t * drift) % WIDTH
        ys = (state[:, 1] + state[:, 3] * t * drift) % HEIGHT
        radii = state[:, 4]
        alphas = state[:, 5]

        for i in range(n):
            cx, cy = float(xs[i]), float(ys[i])
            r = float(radii[i])
            a = float(alphas[i])
            # 3x3 box around centre, gaussian-ish falloff in 8-bit space.
            x0 = max(0, int(cx - r))
            x1 = min(WIDTH, int(cx + r) + 1)
            y0 = max(0, int(cy - r))
            y1 = min(HEIGHT, int(cy + r) + 1)
            if x1 <= x0 or y1 <= y0:
                continue
            sub_y = np.arange(y0, y1)[:, None]
            sub_x = np.arange(x0, x1)[None, :]
            d2 = (sub_x - cx) ** 2 + (sub_y - cy) ** 2
            mask = np.exp(-d2 / max(0.4, r * r)) * a
            # Tint toward warm gold.
            for c, ch in enumerate(GOLD_SOFT):
                frame[y0:y1, x0:x1, c] = np.clip(
                    frame[y0:y1, x0:x1, c] + (mask * (ch - frame[y0:y1, x0:x1, c]) * 0.5).astype(np.int16),
                    0, 255,
                )
        return frame.astype(np.uint8)

    return VideoClip(make_frame, duration=duration).set_fps(FPS)


def _asset_background_clip(duration: float) -> Optional[VideoClip]:
    """If a real background asset exists, render it scaled to 1080x1920."""
    if not CFG.get("use_assets", True):
        return None
    picked = _pick_background()
    if picked is None:
        return None
    kind, path = picked
    try:
        if kind == "video":
            clip = VideoFileClip(str(path), audio=False)
            # Loop or trim to duration.
            if clip.duration < duration:
                from moviepy.video.fx.loop import loop as _loop
                clip = _loop(clip, duration=duration)
            else:
                clip = clip.subclip(0, duration)
            clip = clip.resize(height=HEIGHT)
            if clip.w < WIDTH:
                clip = clip.resize(width=WIDTH)
            # Centre-crop to 1080x1920.
            x_centre = clip.w / 2
            clip = clip.crop(x_center=x_centre, y_center=clip.h / 2,
                             width=WIDTH, height=HEIGHT)
            # Mute audio (we add our own music separately).
            clip = clip.without_audio().fx(lambda c: c)
            return clip
        else:
            img = Image.open(path).convert("RGB")
            ratio_target = WIDTH / HEIGHT
            ratio_img = img.width / img.height
            if ratio_img > ratio_target:
                # crop sides
                new_w = int(img.height * ratio_target)
                x0 = (img.width - new_w) // 2
                img = img.crop((x0, 0, x0 + new_w, img.height))
            else:
                new_h = int(img.width / ratio_target)
                y0 = (img.height - new_h) // 2
                img = img.crop((0, y0, img.width, y0 + new_h))
            img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
            # Darken the image so text stays readable.
            arr = np.array(img).astype(np.int16) * 0.55
            arr = arr.astype(np.uint8)
            return ImageClip(arr).set_duration(duration)
    except Exception as e:
        print(f"[asset-bg] failed to load {path.name}: {e}", flush=True)
        return None


def _scene_background(
    duration: float,
    glow_cx: float = 0.5,
    glow_cy: float = 0.42,
) -> VideoClip:
    """Return a background VideoClip for one scene. Prefers asset bg, then
    animated particles, then a plain still."""
    asset = _asset_background_clip(duration)
    if asset is not None:
        return asset
    if CFG.get("fallback_animated_background", True):
        return _animated_background_clip(duration, glow_cx=glow_cx, glow_cy=glow_cy)
    still_arr = np.array(_make_still_background(glow_cx, glow_cy).convert("RGB"))
    return ImageClip(still_arr).set_duration(duration)


# ---------------------------------------------------------------------------
# Drawing helpers (work on an overlay PIL image, not the background)
# ---------------------------------------------------------------------------
def _new_overlay() -> Image.Image:
    return Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> Tuple[int, int, int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox  # left, top, right, bottom


def _draw_centered_text(
    img: Image.Image,
    text: str,
    font: ImageFont.FreeTypeFont,
    y: int,
    color=WHITE,
    max_chars_per_line: int = 18,
    line_spacing: int = 18,
    shadow: int = 2,
) -> int:
    draw = ImageDraw.Draw(img)
    lines: List[str] = []
    for paragraph in text.split("\n"):
        wrapped = textwrap.wrap(paragraph, width=max_chars_per_line) or [""]
        lines.extend(wrapped)

    cur_y = y
    for line in lines:
        bbox = _text_size(draw, line, font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        x = (WIDTH - w) // 2
        if shadow:
            draw.text((x + shadow, cur_y + shadow), line, font=font, fill=(0, 0, 0, 255))
        draw.text((x, cur_y), line, font=font, fill=color)
        cur_y += h + line_spacing
    return cur_y


def _draw_tracked_text(
    img: Image.Image,
    text: str,
    font: ImageFont.FreeTypeFont,
    y: int,
    color=WHITE,
    tracking: int = 10,
    shadow: int = 1,
) -> None:
    draw = ImageDraw.Draw(img)
    widths = []
    for ch in text:
        bbox = _text_size(draw, ch, font)
        widths.append(bbox[2] - bbox[0])
    total = sum(widths) + tracking * max(0, len(text) - 1)
    x = (WIDTH - total) // 2
    for ch, w in zip(text, widths):
        if shadow:
            draw.text((x + shadow, y + shadow), ch, font=font, fill=(0, 0, 0, 255))
        draw.text((x, y), ch, font=font, fill=color)
        x += w + tracking


def _draw_gold_underline(img: Image.Image, y: int, width: int = 320, color=GOLD, thickness: int = 2) -> None:
    draw = ImageDraw.Draw(img, "RGBA")
    cx = WIDTH // 2
    draw.rectangle((cx - width // 2, y, cx + width // 2, y + thickness), fill=color)


def _draw_separator(
    img: Image.Image,
    y: int,
    color=GOLD,
    line_len: int = 110,
    gap: int = 16,
    diamond: int = 6,
    thickness: int = 2,
) -> None:
    draw = ImageDraw.Draw(img, "RGBA")
    cx = WIDTH // 2
    line_y = y + thickness // 2
    draw.rectangle((cx - diamond - gap - line_len, y, cx - diamond - gap, y + thickness), fill=color)
    draw.rectangle((cx + diamond + gap, y, cx + diamond + gap + line_len, y + thickness), fill=color)
    draw.polygon(
        [(cx, line_y - diamond), (cx + diamond, line_y), (cx, line_y + diamond), (cx - diamond, line_y)],
        fill=color,
    )


def _draw_pill(
    img: Image.Image,
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    padding_x: int = 46,
    padding_y: int = 20,
    fill=(18, 14, 22, 215),
    border=GOLD,
    border_w: int = 2,
    text_color=WHITE,
) -> None:
    draw = ImageDraw.Draw(img, "RGBA")
    bbox = _text_size(draw, text, font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pill_w = tw + padding_x * 2
    pill_h = th + padding_y * 2
    x = (WIDTH - pill_w) // 2
    radius = pill_h // 2
    draw.rounded_rectangle((x, y, x + pill_w, y + pill_h), radius=radius,
                           fill=fill, outline=border, width=border_w)
    text_x = x + padding_x - bbox[0]
    text_y = y + (pill_h - th) // 2 - bbox[1]
    draw.text((text_x, text_y), text, font=font, fill=text_color)


def _draw_top_brand_chip(img: Image.Image, brand: str) -> None:
    """Small brand mark + side ticks at the top, inside the safe margin."""
    chip_font = _load_font(24, bold=True, sample=brand)
    draw = ImageDraw.Draw(img, "RGBA")
    y = max(SAFE_MARGIN // 2 + 30, 80)
    # Centred wordmark
    bbox = _text_size(draw, brand, chip_font)
    tw = bbox[2] - bbox[0]
    x = (WIDTH - tw) // 2
    draw.text((x, y), brand, font=chip_font, fill=GOLD)
    # Small gold dots either side
    cx = WIDTH // 2
    dot_r = 3
    dx = tw // 2 + 26
    draw.ellipse((cx - dx - dot_r, y + 12 - dot_r, cx - dx + dot_r, y + 12 + dot_r), fill=GOLD_DIM)
    draw.ellipse((cx + dx - dot_r, y + 12 - dot_r, cx + dx + dot_r, y + 12 + dot_r), fill=GOLD_DIM)


# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------
def _chunk_words(text: str, max_words: int) -> List[str]:
    text = text.strip()
    if not text:
        return []
    words = text.split()
    if not words:
        return [text]
    chunks: List[str] = []
    i = 0
    while i < len(words):
        chunks.append(" ".join(words[i:i + max_words]))
        i += max_words
    return chunks


def _split_script(script: str, max_words_per_screen: Optional[int] = None) -> List[str]:
    """Split a script into short on-screen lines."""
    max_words = max_words_per_screen if max_words_per_screen else int(CFG.get("max_words_per_screen", 6))
    script = script.strip()
    if not script:
        return [""]
    # Sentence-first split (keeps punctuation cadence), then word-pack within sentence.
    sentences = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", script) if s.strip()]
    if not sentences:
        sentences = [script]
    out: List[str] = []
    for s in sentences:
        # Strip trailing punctuation for on-screen text but keep commas/em-dashes.
        cleaned = s.rstrip(".!?…")
        # If a sentence has clear commas, prefer those as natural breakpoints.
        if "," in cleaned:
            comma_parts = [p.strip(" ,") for p in cleaned.split(",") if p.strip(" ,")]
            for part in comma_parts:
                out.extend(_chunk_words(part, max_words))
        else:
            out.extend(_chunk_words(cleaned, max_words))
    # Cap at 6 to keep the video short.
    return [c for c in out if c][:6]


def _split_cta(cta: str, brand: str) -> Tuple[str, str]:
    text = cta.strip()
    brand_u = brand.strip().upper()
    suffixes = [
        f" на {brand_u}", f" на {brand}",
        f" в {brand_u}", f" в {brand}",
        f" {brand_u}", f" {brand}",
    ]
    for suf in suffixes:
        if text.lower().endswith(suf.lower()):
            return text[: -len(suf)].rstrip(" ,.—-"), brand_u
    return text, brand_u


# ---------------------------------------------------------------------------
# Foreground frames (overlay PIL images, transparent background)
# ---------------------------------------------------------------------------
def _frame_hook_overlay(hook: str, brand: str) -> Image.Image:
    img = _new_overlay()
    hook_font = _load_font(98, bold=True, sample=hook)

    _draw_top_brand_chip(img, brand)

    # Top ornament — single gold dot above the hook.
    draw = ImageDraw.Draw(img, "RGBA")
    cx = WIDTH // 2
    dot_r = 5
    dot_y = int(HEIGHT * 0.34)
    draw.ellipse((cx - dot_r, dot_y - dot_r, cx + dot_r, dot_y + dot_r), fill=GOLD)

    _draw_centered_text(
        img, hook, hook_font,
        y=int(HEIGHT * 0.40),
        color=WHITE, max_chars_per_line=11, line_spacing=14, shadow=3,
    )
    _draw_gold_underline(img, y=int(HEIGHT * 0.66), width=320, color=GOLD)
    return img


def _frame_chunk_overlay(chunk: str, brand: str, idx: int) -> Image.Image:
    img = _new_overlay()
    body_font = _load_font(80, italic=True, sample=chunk)

    _draw_top_brand_chip(img, brand)

    draw = ImageDraw.Draw(img, "RGBA")
    tick_y = int(HEIGHT * 0.44)
    tick_w = 60
    tick_h = 2
    draw.rectangle((SAFE_MARGIN, tick_y, SAFE_MARGIN + tick_w, tick_y + tick_h), fill=GOLD)
    draw.rectangle((WIDTH - SAFE_MARGIN - tick_w, tick_y, WIDTH - SAFE_MARGIN, tick_y + tick_h), fill=GOLD)

    _draw_centered_text(
        img, chunk, body_font,
        y=int(HEIGHT * 0.46),
        color=WHITE, max_chars_per_line=18, line_spacing=12, shadow=2,
    )
    # Small index dot row in the lower third (visual rhythm).
    if idx >= 0:
        dots_y = int(HEIGHT * 0.68)
        dot_r = 4
        spacing = 18
        cx = WIDTH // 2
        for i in range(5):
            x = cx + (i - 2) * spacing
            color = GOLD if i == (idx % 5) else (90, 70, 40)
            draw.ellipse((x - dot_r, dots_y - dot_r, x + dot_r, dots_y + dot_r), fill=color)
    return img


def _frame_spokesperson_overlay(brand: str, voiceover_hint: str) -> Image.Image:
    """Placeholder for a UGC / spokesperson clip. Designed to be replaced by
    a face-cam in CapCut, but already looks intentional on its own."""
    img = _new_overlay()
    label_font = _load_font(28, italic=True, sample="голос аромата")
    quote_font = _load_font(62, italic=True, sample=voiceover_hint or "TREZZY")

    _draw_top_brand_chip(img, brand)
    _draw_centered_text(
        img, "голос аромата", label_font,
        y=int(HEIGHT * 0.30),
        color=SOFT, max_chars_per_line=24, line_spacing=0, shadow=1,
    )
    _draw_separator(img, y=int(HEIGHT * 0.34), line_len=80, diamond=5)

    txt = voiceover_hint.strip() or "ты ещё не пробовал такие"
    _draw_centered_text(
        img, "«" + txt + "»", quote_font,
        y=int(HEIGHT * 0.42),
        color=WHITE, max_chars_per_line=18, line_spacing=10, shadow=2,
    )

    # Footer hint for the editor — drawn very small, dim, just below safe area.
    hint_font = _load_font(22, sample="add face cam here")
    _draw_centered_text(
        img, "[ заменить на UGC / AI-аватар в CapCut ]", hint_font,
        y=int(HEIGHT * 0.92),
        color=(110, 90, 70), max_chars_per_line=40, line_spacing=0, shadow=0,
    )
    return img


def _frame_product_vibe_overlay(brand: str, tags: List[str]) -> Image.Image:
    img = _new_overlay()
    label_font = _load_font(30, italic=True, sample="ноты аромата")

    _draw_top_brand_chip(img, brand)
    _draw_centered_text(
        img, "ноты аромата", label_font,
        y=int(HEIGHT * 0.24),
        color=SOFT, max_chars_per_line=24, line_spacing=0, shadow=1,
    )
    _draw_separator(img, y=int(HEIGHT * 0.28), line_len=80, diamond=5)
    return img


def _frame_pill_overlay_only(text: str, y: int, sample_full: str) -> Image.Image:
    img = _new_overlay()
    pill_font = _load_font(48, sample=sample_full)
    _draw_pill(img, y, text, pill_font, padding_x=54, padding_y=22)
    return img


def _frame_proof_overlay(brand: str, proof_text: str) -> Image.Image:
    img = _new_overlay()
    font = _load_font(54, italic=True, sample=proof_text)

    _draw_top_brand_chip(img, brand)
    _draw_centered_text(
        img, proof_text, font,
        y=int(HEIGHT * 0.40),
        color=WHITE, max_chars_per_line=22, line_spacing=14, shadow=2,
    )
    _draw_gold_underline(img, y=int(HEIGHT * 0.62), width=240, color=GOLD_DIM)
    return img


def _frame_cta_overlay(cta_action: str, brand: str) -> Image.Image:
    img = _new_overlay()
    action_font = _load_font(54, italic=True, sample=cta_action or "")
    brand_font = _load_font(120, bold=True, sample=brand)
    url_font = _load_font(28, sample="trezzy.shop")

    draw = ImageDraw.Draw(img, "RGBA")
    cx = WIDTH // 2

    if cta_action:
        _draw_centered_text(
            img, cta_action, action_font,
            y=int(HEIGHT * 0.34),
            color=WHITE, max_chars_per_line=18, line_spacing=12, shadow=2,
        )

    top_line_y = int(HEIGHT * 0.46)
    bot_line_y = int(HEIGHT * 0.60)
    line_w = 420
    draw.rectangle((cx - line_w // 2, top_line_y, cx + line_w // 2, top_line_y + 2), fill=GOLD_DIM)
    draw.rectangle((cx - line_w // 2, bot_line_y, cx + line_w // 2, bot_line_y + 2), fill=GOLD_DIM)

    _draw_tracked_text(img, brand, brand_font, y=int(HEIGHT * 0.49), color=GOLD, tracking=28, shadow=2)
    _draw_centered_text(
        img, "trezzy.shop", url_font,
        y=int(HEIGHT * 0.65),
        color=SOFT, max_chars_per_line=30, line_spacing=0, shadow=1,
    )
    return img


# ---------------------------------------------------------------------------
# Compose helpers
# ---------------------------------------------------------------------------
def _pil_to_overlay_clip(rgba_img: Image.Image, duration: float):
    arr = np.array(rgba_img)
    if arr.shape[-1] == 4:
        rgb = np.ascontiguousarray(arr[..., :3])
        alpha = arr[..., 3].astype(np.float32) / 255.0
        clip = ImageClip(rgb).set_duration(duration)
        mask = ImageClip(alpha, ismask=True).set_duration(duration)
        return clip.set_mask(mask)
    return ImageClip(arr).set_duration(duration)


def _kenburns(clip, duration: float, zoom_in: bool = True, amount: float = 0.04):
    if zoom_in:
        return clip.resize(lambda t, d=duration, a=amount: 1.0 + a * (t / d))
    return clip.resize(lambda t, d=duration, a=amount: (1.0 + a) - a * (t / d))


def _perfume_layer(duration: float, slot: str = "centre") -> Optional[VideoClip]:
    """Place a perfume product image, if available, into the scene."""
    if not CFG.get("use_assets", True):
        return None
    path = _pick_perfume_image()
    if path is None:
        return None
    try:
        img = Image.open(path).convert("RGBA")
        target_h = int(HEIGHT * 0.42)
        ratio = target_h / img.height
        new_w = int(img.width * ratio)
        img = img.resize((new_w, target_h), Image.LANCZOS)
        # Bake onto a transparent 1080x1920 canvas in the requested slot.
        canvas = _new_overlay()
        if slot == "right":
            x = WIDTH - new_w - SAFE_MARGIN
            y = int(HEIGHT * 0.36)
        else:
            x = (WIDTH - new_w) // 2
            y = int(HEIGHT * 0.42)
        canvas.alpha_composite(img, (x, y))
        clip = _pil_to_overlay_clip(canvas, duration)
        # Gentle drift to make it feel alive.
        return _kenburns(clip, duration, zoom_in=True, amount=0.02)
    except Exception as e:
        print(f"[perfume] failed: {e}", flush=True)
        return None


def _overlay_layer(duration: float) -> Optional[VideoClip]:
    """Add a single overlay (smoke / leak / glow) as a screen-blended layer."""
    if not CFG.get("use_assets", True):
        return None
    picked = _pick_overlay()
    if picked is None:
        return None
    kind, path = picked
    try:
        if kind == "video":
            ov = VideoFileClip(str(path), audio=False)
            if ov.duration < duration:
                from moviepy.video.fx.loop import loop as _loop
                ov = _loop(ov, duration=duration)
            else:
                ov = ov.subclip(0, duration)
            ov = ov.resize(height=HEIGHT)
            if ov.w < WIDTH:
                ov = ov.resize(width=WIDTH)
            ov = ov.crop(x_center=ov.w / 2, y_center=ov.h / 2, width=WIDTH, height=HEIGHT)
            # Treat dark pixels as transparent (cheap "screen blend").
            return ov.set_opacity(0.45)
        else:
            img = Image.open(path).convert("RGBA").resize((WIDTH, HEIGHT), Image.LANCZOS)
            return _pil_to_overlay_clip(img, duration).set_opacity(0.45)
    except Exception as e:
        print(f"[overlay] failed: {e}", flush=True)
        return None


def _compose_scene(
    duration: float,
    overlay_pil: Image.Image,
    fade: float,
    glow_cx: float = 0.5,
    glow_cy: float = 0.42,
    zoom_in: bool = True,
    zoom_amount: float = 0.035,
    extra_layers: Optional[List[VideoClip]] = None,
):
    bg = _scene_background(duration, glow_cx=glow_cx, glow_cy=glow_cy)
    bg = _kenburns(bg, duration, zoom_in=zoom_in, amount=zoom_amount)
    bg = bg.set_position("center")

    overlay_clip = _pil_to_overlay_clip(overlay_pil, duration).set_position("center")

    layers: List[VideoClip] = [bg]
    leak = _overlay_layer(duration)
    if leak is not None:
        layers.append(leak.set_position("center"))
    if extra_layers:
        layers.extend(extra_layers)
    layers.append(overlay_clip)

    scene = CompositeVideoClip(layers, size=(WIDTH, HEIGHT)).set_duration(duration)
    return scene.crossfadein(fade).crossfadeout(fade)


# ---------------------------------------------------------------------------
# Scenes
# ---------------------------------------------------------------------------
def _scene_hook(hook: str, brand: str, duration: float, fade: float):
    overlay = _frame_hook_overlay(hook, brand)
    return _compose_scene(
        duration, overlay, fade,
        glow_cx=0.50, glow_cy=0.42,
        zoom_in=True, zoom_amount=0.05,
    )


def _scene_spokesperson(brand: str, voiceover_hint: str, duration: float, fade: float):
    overlay = _frame_spokesperson_overlay(brand, voiceover_hint)
    return _compose_scene(
        duration, overlay, fade,
        glow_cx=0.55, glow_cy=0.40,
        zoom_in=False, zoom_amount=0.03,
    )


def _scene_main_chunks(chunks: List[str], brand: str, duration: float, fade: float, sub_fade: float = 0.22):
    if not chunks:
        # Defensive fallback.
        chunks = [""]
    n = len(chunks)
    chunk_dur = (duration + (n - 1) * sub_fade) / n

    glow_x_seq = [0.40, 0.60, 0.50, 0.45, 0.55, 0.50]
    chunk_clips = []
    for i, ch in enumerate(chunks):
        overlay = _frame_chunk_overlay(ch, brand, idx=i)
        scene = _compose_scene(
            chunk_dur, overlay, sub_fade,
            glow_cx=glow_x_seq[i % len(glow_x_seq)],
            glow_cy=0.46,
            zoom_in=(i % 2 == 0),
            zoom_amount=0.025,
        )
        chunk_clips.append(scene)

    sequence = concatenate_videoclips(chunk_clips, method="compose", padding=-sub_fade)
    # Wrap the sequence on a 1080x1920 canvas with overall fade in/out for clean joins.
    base = ColorClip(size=(WIDTH, HEIGHT), color=BG_COLOR).set_duration(duration)
    wrapped = CompositeVideoClip(
        [base, sequence.set_position("center")],
        size=(WIDTH, HEIGHT),
    ).set_duration(duration)
    return wrapped.crossfadein(fade).crossfadeout(fade)


def _scene_product_vibe(brand: str, tags: List[str], duration: float, fade: float):
    overlay = _frame_product_vibe_overlay(brand, tags)
    pill_ys = [int(HEIGHT * 0.40), int(HEIGHT * 0.54), int(HEIGHT * 0.68)]
    pill_starts = [0.25, 0.85, 1.45]
    sample_full = " ".join(tags) + "abc"

    extras: List[VideoClip] = []
    # Optional perfume image to the side of the pills (right slot).
    perfume = _perfume_layer(duration, slot="right")
    if perfume is not None:
        extras.append(perfume.set_position("center"))

    for i, tag in enumerate(tags[:3]):
        start_t = pill_starts[i]
        if start_t >= duration:
            continue
        pill_img = _frame_pill_overlay_only(tag, pill_ys[i], sample_full=sample_full)
        pclip = _pil_to_overlay_clip(pill_img, duration - start_t)
        pclip = pclip.set_start(start_t).crossfadein(0.30).set_position("center")
        extras.append(pclip)

    return _compose_scene(
        duration, overlay, fade,
        glow_cx=0.50, glow_cy=0.50,
        zoom_in=True, zoom_amount=0.02,
        extra_layers=extras,
    )


def _scene_proof(brand: str, proof_text: str, duration: float, fade: float):
    overlay = _frame_proof_overlay(brand, proof_text)
    return _compose_scene(
        duration, overlay, fade,
        glow_cx=0.45, glow_cy=0.48,
        zoom_in=True, zoom_amount=0.025,
    )


def _scene_cta(cta_action: str, brand: str, duration: float, fade: float):
    overlay = _frame_cta_overlay(cta_action, brand)
    return _compose_scene(
        duration, overlay, fade,
        glow_cx=0.50, glow_cy=0.42,
        zoom_in=False, zoom_amount=0.04,
    )


# ---------------------------------------------------------------------------
# Scene plans by format
# ---------------------------------------------------------------------------
_UGC_FORMATS = {"ai_ugc_ad"}


def _voiceover_hint(chunks: List[str], hook: str) -> str:
    """Pick a short phrase to put in the spokesperson scene quote box."""
    candidates = [c for c in chunks if 3 <= len(c.split()) <= 7]
    if candidates:
        return candidates[0]
    return hook


def _proof_line(chunks: List[str], hook: str, tags: List[str]) -> str:
    """Pick or synthesise a proof / feeling line for the proof scene."""
    if chunks:
        # Prefer the longest punchy chunk that isn't the same as the hook.
        ranked = sorted(chunks, key=lambda c: (-len(c.split()), c))
        for c in ranked:
            if c.lower() != hook.lower():
                return c
    if tags:
        return tags[0] + ". " + (tags[1] if len(tags) > 1 else "")
    return hook


def _build_video(
    hook: str,
    brand: str,
    script: str,
    vibe_tags: List[str],
    cta: str,
    out_path: Path,
    format_hint: Optional[str] = None,
) -> float:
    fmt = (format_hint or "").lower()
    is_ugc = fmt in _UGC_FORMATS

    dur_cfg = CFG.get("scene_durations", {})
    fade = float(CFG.get("ugc_fade_seconds" if is_ugc else "fade_seconds", 0.30))

    chunks = _split_script(script, max_words_per_screen=int(CFG.get("max_words_per_screen", 6)))
    if not chunks:
        chunks = [script.strip() or hook]

    voiceover_hint = _voiceover_hint(chunks, hook)
    action_line, brand_u = _split_cta(cta, brand)

    # --- Decide scene set ---
    HOOK_DUR  = float(dur_cfg.get("hook", 2.4))
    SPK_DUR   = float(dur_cfg.get("spokesperson", 2.2))
    PROD_DUR  = float(dur_cfg.get("product_vibe", 3.4))
    PROOF_DUR = float(dur_cfg.get("proof_feeling", 2.8))
    CTA_DUR   = float(dur_cfg.get("cta", 2.6))
    PREM_MAIN = float(dur_cfg.get("premium_main", 5.2))
    UGC_CHUNK = float(dur_cfg.get("ugc_chunk", 1.4))

    scenes: List = []

    # 1) HOOK
    scenes.append(_scene_hook(hook, brand_u, HOOK_DUR, fade))

    # 2) SPOKESPERSON placeholder (always - it slots into UGC and luxury alike).
    scenes.append(_scene_spokesperson(brand_u, voiceover_hint, SPK_DUR, fade))

    # 3) MAIN body
    if is_ugc:
        # Each chunk gets its own short scene → fast cuts.
        main_chunks = chunks[:5] if len(chunks) >= 5 else chunks
        total_main_dur = max(UGC_CHUNK * len(main_chunks), 2.6)
        scenes.append(_scene_main_chunks(main_chunks, brand_u, total_main_dur, fade, sub_fade=fade))
    else:
        scenes.append(_scene_main_chunks(chunks, brand_u, PREM_MAIN, fade))

    # 4) PRODUCT VIBE
    scenes.append(_scene_product_vibe(brand_u, vibe_tags, PROD_DUR, fade))

    # 5) PROOF / FEELING
    proof_text = _proof_line(chunks, hook, vibe_tags)
    scenes.append(_scene_proof(brand_u, proof_text, PROOF_DUR, fade))

    # 6) CTA
    scenes.append(_scene_cta(action_line, brand_u, CTA_DUR, fade))

    # --- Concatenate ---
    sequence = concatenate_videoclips(scenes, method="compose", padding=-fade)
    total_raw = sum(s.duration for s in scenes)
    final_duration = total_raw - fade * (len(scenes) - 1)

    base = ColorClip(size=(WIDTH, HEIGHT), color=BG_COLOR).set_duration(total_raw)
    final = CompositeVideoClip(
        [base, sequence.set_position("center")],
        size=(WIDTH, HEIGHT),
    ).set_duration(final_duration)

    # --- Optional music ---
    audio_clip = None
    music_path = _pick_music() if CFG.get("enable_music", True) and CFG.get("use_assets", True) else None
    if music_path is not None:
        try:
            audio_clip = AudioFileClip(str(music_path))
            # Loop / trim audio to video duration.
            from moviepy.audio.fx.audio_loop import audio_loop
            if audio_clip.duration < final_duration:
                audio_clip = audio_loop(audio_clip, duration=final_duration)
            else:
                audio_clip = audio_clip.subclip(0, final_duration)
            vol = float(CFG.get("music_volume", 0.35))
            audio_clip = audio_clip.volumex(vol).audio_fadein(0.6).audio_fadeout(0.8)
            final = final.set_audio(audio_clip)
        except Exception as e:
            print(f"[music] failed to attach {music_path.name}: {e}", flush=True)
            audio_clip = None

    has_audio = audio_clip is not None
    final.write_videofile(
        str(out_path),
        fps=FPS,
        codec="libx264",
        audio=has_audio,
        audio_codec="aac" if has_audio else None,
        preset="medium",
        bitrate="6000k",
        threads=4,
        verbose=False,
        logger=None,
    )
    duration = final.duration
    final.close()
    if audio_clip is not None:
        try:
            audio_clip.close()
        except Exception:
            pass
    return duration


# ---------------------------------------------------------------------------
# Edit notes / CapCut checklist
# ---------------------------------------------------------------------------
def _assets_inventory_line() -> str:
    found = {sub: len(_list_assets(sub, FONT_EXTS if sub == "fonts" else (
        AUDIO_EXTS if sub == "music" else IMAGE_EXTS | VIDEO_EXTS
    ))) for sub in ASSET_SUBDIRS}
    parts = [f"{k}={v}" for k, v in found.items()]
    return "Assets detected: " + ", ".join(parts)


def _build_edit_notes(norm: dict, plan: Optional[dict] = None) -> str:
    extra = ""
    if plan and plan.get("edit_notes"):
        extra = (
            "\n\nFormat-specific guidance (from plan)\n"
            "------------------------------------\n"
            f"Format: {plan.get('format', 'unknown')}\n"
            f"{plan['edit_notes']}\n"
        )
    return (
        "TREZZY content edit notes\n"
        "=========================\n\n"
        "Brand voice: premium, dark, elegant, mature. Not loud. Not startup-y.\n\n"
        f"Hook : {norm['hook']}\n"
        f"Tags : {', '.join(norm['vibe_tags'])}\n"
        f"CTA  : {norm['cta']}\n\n"
        f"{_assets_inventory_line()}\n\n"
        "Scenes baked into final.mp4\n"
        "---------------------------\n"
        "1) HOOK - large headline, gold accent line\n"
        "2) SPOKESPERSON placeholder - REPLACE with AI avatar / UGC face cam in CapCut\n"
        "3) MAIN - short text chunks (max 6 words/screen), tick-mark frames\n"
        "4) PRODUCT VIBE - pill tags + optional perfume image\n"
        "5) PROOF / FEELING - one premium emotional line\n"
        "6) CTA - tracked brand wordmark + trezzy.shop\n\n"
        "Look & feel\n"
        "-----------\n"
        "- Dark luxury background, warm gold accents only.\n"
        "- Drifting gold particles + vignette + film grain are already baked.\n"
        "- Subtle Ken Burns motion per scene. No flashy cuts.\n"
        "- Serif headlines, italic for body. No bouncy fonts.\n\n"
        "In CapCut, add manually\n"
        "-----------------------\n"
        "- AI spokesperson / avatar clip over scene 2 (the placeholder).\n"
        "- CapCut auto-captions for the voiceover (white sans, gold underline).\n"
        "- Real perfume b-roll between MAIN and PRODUCT VIBE (1.5-2.5s clip).\n"
        "- Voiceover (RU, calm low-pitch) over the whole timeline.\n"
        "- Premium transitions only (fade, dissolve, light leak). No spin / glitch.\n"
        "- Music: cinematic / niche perfume ambient at -18 LUFS, ducked -6 dB.\n\n"
        "Don't\n"
        "-----\n"
        "- No \"discover the magic\", \"amazing\", \"incredible\" copy.\n"
        "- No stock perfume bottle clipart overlays.\n"
        "- No bouncy / spring transitions.\n"
        "- No bright pink / cyan accents.\n"
        + extra
    )


def _build_capcut_checklist(norm: dict) -> str:
    return (
        "# CapCut hand-off checklist - TREZZY short\n\n"
        "Source video: `final.mp4` (this folder)\n"
        f"Hook: **{norm['hook']}**\n"
        f"CTA:  **{norm['cta']}**\n"
        f"Tags: {', '.join(norm['vibe_tags'])}\n\n"
        "## Import\n"
        "- [ ] Drag `final.mp4` onto the CapCut timeline.\n"
        "- [ ] Project canvas: **1080 x 1920** (9:16).\n"
        "- [ ] Frame rate: 30 fps.\n\n"
        "## Replace the spokesperson placeholder\n"
        "- [ ] Locate scene 2 (the `[ заменить на UGC / AI-аватар ]` slate).\n"
        "- [ ] Drop in an AI spokesperson clip (HeyGen / D-ID / Captions.ai) or real UGC face cam.\n"
        "- [ ] Trim to ~2 seconds. Keep speaker eyeline centred.\n\n"
        "## Add perfume b-roll\n"
        "- [ ] Add 1-2 short perfume close-ups between MAIN and PRODUCT VIBE.\n"
        "- [ ] Length 1.5-2.5s each. Slow push-in or rack focus.\n"
        "- [ ] Match colour to the dark/gold base (warm temp, slight crush).\n\n"
        "## Audio\n"
        "- [ ] Add voiceover for the script (RU, calm, low-pitch). One take per chunk.\n"
        "- [ ] Add music: cinematic / niche-perfume ambient. Target -18 LUFS.\n"
        "- [ ] Duck music under voiceover (-6 dB).\n\n"
        "## Captions\n"
        "- [ ] Run CapCut auto-captions on the voiceover.\n"
        "- [ ] Style: white sans-serif, gold underline, centred, bottom third.\n"
        "- [ ] No emoji in captions. Max 6 words per caption screen.\n\n"
        "## Transitions\n"
        "- [ ] Premium only: fade / dissolve / light leak. No spin, whip pan, glitch.\n"
        "- [ ] Keep transitions under 0.4s.\n\n"
        "## Premium effects (use sparingly)\n"
        "- [ ] Film grain 5-10% (we already bake some - don't double up).\n"
        "- [ ] Light leak overlay on HOOK and CTA only.\n"
        "- [ ] Vignette pass.\n\n"
        "## Export\n"
        "- [ ] 1080 x 1920 MP4, H.264, 8-10 Mbps.\n"
        "- [ ] AAC audio 192 kbps.\n"
        "- [ ] File name: `trezzy_<topic>_v1.mp4`.\n"
    )


# ---------------------------------------------------------------------------
# Package writer
# ---------------------------------------------------------------------------
def _write_package(
    norm: dict,
    duration: float,
    created_at: str,
    plan: Optional[dict] = None,
) -> None:
    (LATEST_DIR / "script.txt").write_text(norm["script"], encoding="utf-8")
    (LATEST_DIR / "caption.txt").write_text(norm["caption"], encoding="utf-8")
    (LATEST_DIR / "hashtags.txt").write_text("\n".join(norm["hashtags"]) + "\n", encoding="utf-8")
    (LATEST_DIR / "edit_notes.txt").write_text(_build_edit_notes(norm, plan=plan), encoding="utf-8")
    (LATEST_DIR / "capcut_checklist.md").write_text(_build_capcut_checklist(norm), encoding="utf-8")

    payload = {
        "hook":              norm["hook"],
        "title":             norm["brand"],
        "script":            norm["script"],
        "vibe_tags":         norm["vibe_tags"],
        "cta":               norm["cta"],
        "caption":           norm["caption"],
        "hashtags":          norm["hashtags"],
        "format":            norm.get("format"),
        "duration_seconds":  round(duration, 2),
        "created_at":        created_at,
    }
    (LATEST_DIR / "request.json").write_text(
        json_lib.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if plan is not None:
        (LATEST_DIR / "plan.json").write_text(
            json_lib.dumps(plan, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
def _utf8_json(payload: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content=payload,
        status_code=status_code,
        media_type="application/json; charset=utf-8",
    )


@app.get("/")
def root():
    return _utf8_json({"service": "TREZZY Video Worker", "status": "ok", "version": "0.4.0"})


@app.get("/health")
def health():
    return _utf8_json({"status": "ok", "service": "trezzy-video-worker"})


def _run_generation(norm: dict, plan: Optional[dict] = None) -> dict:
    chunks = _split_script(norm["script"])
    action, brand_u = _split_cta(norm["cta"], norm["brand"])
    print("[generate] hook  =", repr(norm["hook"]), flush=True)
    print("[generate] brand =", repr(norm["brand"]), flush=True)
    for i, c in enumerate(chunks, 1):
        print(f"[generate] chunk {i}/{len(chunks)} = {c!r}", flush=True)
    print("[generate] tags  =", norm["vibe_tags"], flush=True)
    print("[generate] cta   =", repr(action), "+ brand =", repr(brand_u), flush=True)
    print("[generate] format=", norm.get("format"), flush=True)
    print("[generate]", _assets_inventory_line(), flush=True)

    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if LATEST_DIR.exists():
        shutil.rmtree(LATEST_DIR)
    LATEST_DIR.mkdir(parents=True, exist_ok=True)

    latest_mp4 = LATEST_DIR / "final.mp4"
    fmt = norm.get("format") or (plan.get("format") if plan else None)

    duration = _build_video(
        hook=norm["hook"],
        brand=norm["brand"],
        script=norm["script"],
        vibe_tags=norm["vibe_tags"],
        cta=norm["cta"],
        out_path=latest_mp4,
        format_hint=fmt,
    )

    canonical = OUTPUT_DIR / "final.mp4"
    if canonical.exists():
        canonical.unlink()
    shutil.copyfile(latest_mp4, canonical)

    _write_package(norm, duration, created_at, plan=plan)

    return {
        "status":            "success",
        "output_path":       str(canonical),
        "package_dir":       str(LATEST_DIR),
        "duration_seconds":  round(duration, 2),
        "created_at":        created_at,
        "caption":           norm["caption"],
        "hashtags":          norm["hashtags"],
    }


@app.post("/generate")
def generate(req: GenerateRequest):
    try:
        norm = _normalize_request(req)
        return _utf8_json(_run_generation(norm))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Video generation failed: {e}")


class PlanRequest(BaseModel):
    topic: str = Field(..., description="What the video is about")
    product_name: Optional[str] = Field(None, description="Specific perfume name (optional)")
    target_audience: Optional[str] = Field(None, description="Who it's for")
    style: Optional[str] = Field("premium luxury perfume", description="Overall style")
    format: Optional[str] = Field(
        "single_review",
        description="single_review | top_list | mood_story | celebrity_style | problem_solution | ai_ugc_ad | ...",
    )
    seed: Optional[int] = Field(None, description="Set for reproducible variations")


def _make_plan_or_raise(req: PlanRequest) -> dict:
    fmt = req.format or "single_review"
    if fmt not in SUPPORTED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"format must be one of {sorted(SUPPORTED_FORMATS)}; got {fmt!r}",
        )
    return make_plan(
        topic=req.topic,
        product_name=req.product_name,
        target_audience=req.target_audience,
        style=req.style or "premium luxury perfume",
        format=fmt,
        seed=req.seed,
    )


@app.post("/plan")
def plan(req: PlanRequest):
    try:
        return _utf8_json(_make_plan_or_raise(req))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Plan generation failed: {e}")


@app.post("/generate-from-plan")
def generate_from_plan(req: PlanRequest):
    try:
        plan_data = _make_plan_or_raise(req)
        gen_req = GenerateRequest(
            hook=plan_data["hook"],
            title=plan_data["title"],
            script=plan_data["script"],
            vibe_tags=plan_data["vibe_tags"],
            cta=plan_data["cta"],
            caption=plan_data["caption"],
            hashtags=plan_data["hashtags"],
            format=plan_data.get("format"),
        )
        norm = _normalize_request(gen_req)
        result = _run_generation(norm, plan=plan_data)
        result["plan"] = plan_data
        return _utf8_json(result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Plan->video generation failed: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
