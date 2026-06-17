# -*- coding: utf-8 -*-
"""Lightweight in-process video renderer for TREZZY (fast mode).

Goal: produce a 1080x1920 vertical MP4 of 5-8 seconds in well under 60s,
WITHOUT calling the separate HTTP video-worker. Uses only Pillow (frame
generation) + a bundled/system ffmpeg (encoding). Full Cyrillic support via
DejaVu Sans (or a Windows font fallback).

Public API:
    render_fast(plan, job_dir) -> dict
        Renders final.mp4 into:
            <job_dir>/final.mp4
            <repo>/output/latest/final.mp4
            <repo>/output/final.mp4
        Returns {"output_path", "duration_seconds", "width", "height", ...}.

This module is intentionally dependency-light and synchronous. The API runs
it inside a thread executor, so blocking here is fine.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
WIDTH = 1080
HEIGHT = 1920
FPS = 30
DURATION_SECONDS = 6  # within the 5-8s requirement
TOTAL_FRAMES = FPS * DURATION_SECONDS

# Luxury palette (deep charcoal -> warm gold)
BG_TOP = (18, 16, 22)
BG_BOTTOM = (40, 32, 28)
GOLD = (212, 175, 110)
GOLD_SOFT = (190, 158, 104)
CREAM = (242, 236, 226)
MUTED = (170, 162, 152)


# ----------------------------------------------------------------------------
# ffmpeg discovery (cross-platform, Windows-friendly)
# ----------------------------------------------------------------------------
def _find_ffmpeg() -> Optional[str]:
    """Locate an ffmpeg binary. Order:
    1. FFMPEG_BINARY env var
    2. ffmpeg on PATH
    3. imageio-ffmpeg's bundled binary (if installed)
    4. common Windows install locations
    """
    env_bin = os.getenv("FFMPEG_BINARY")
    if env_bin and Path(env_bin).exists():
        return env_bin

    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path

    try:
        import imageio_ffmpeg  # type: ignore

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass

    for cand in (
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
    ):
        if cand and Path(cand).exists():
            return cand
    return None


# ----------------------------------------------------------------------------
# Font discovery (must support Cyrillic)
# ----------------------------------------------------------------------------
def _font_candidates() -> List[str]:
    cands: List[str] = []
    env_font = os.getenv("TREZZY_FONT")
    if env_font:
        cands.append(env_font)
    # Windows (full Cyrillic)
    win = os.path.expandvars(r"%WINDIR%\Fonts")
    cands += [
        os.path.join(win, "arialbd.ttf"),
        os.path.join(win, "arial.ttf"),
        os.path.join(win, "segoeui.ttf"),
        os.path.join(win, "calibrib.ttf"),
    ]
    # Linux / DejaVu (full Cyrillic)
    cands += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    return cands


def _load_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    for path in _font_candidates():
        try:
            if path and Path(path).exists():
                # Skip non-bold when bold requested only if a bold alt exists; simple here.
                return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _fit_font(
    draw: "ImageDraw.ImageDraw",
    text: str,
    base_size: int,
    max_w: int,
    bold: bool = True,
    min_size: int = 22,
) -> ImageFont.FreeTypeFont:
    """Return the largest font (<= base_size) whose rendered width fits max_w."""
    size = base_size
    while size > min_size:
        font = _load_font(size, bold)
        w, _ = _measure(draw, text, font)
        if w <= max_w:
            return font
        size -= 2
    return _load_font(min_size, bold)


# ----------------------------------------------------------------------------
# Drawing helpers
# ----------------------------------------------------------------------------
def _vertical_gradient(top: Tuple[int, int, int], bottom: Tuple[int, int, int]) -> Image.Image:
    # Build a 1px-wide gradient column, then resize to full width (fast, no per-pixel loop).
    col = Image.new("RGB", (1, HEIGHT))
    cp = col.load()
    for y in range(HEIGHT):
        t = y / (HEIGHT - 1)
        cp[0, y] = (
            int(top[0] + (bottom[0] - top[0]) * t),
            int(top[1] + (bottom[1] - top[1]) * t),
            int(top[2] + (bottom[2] - top[2]) * t),
        )
    return col.resize((WIDTH, HEIGHT))


def _measure(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> Tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> List[str]:
    words = text.split()
    lines: List[str] = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        tw, _ = _measure(draw, trial, font)
        if tw <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _draw_center_block(
    draw: ImageDraw.ImageDraw,
    lines: List[str],
    font: ImageFont.FreeTypeFont,
    y_center: int,
    fill: Tuple[int, int, int],
    line_gap: int = 18,
    shadow: bool = True,
) -> None:
    sizes = [_measure(draw, ln, font) for ln in lines]
    line_h = max((h for _, h in sizes), default=font.size) + line_gap
    total_h = line_h * len(lines)
    y = y_center - total_h // 2
    for ln, (tw, _) in zip(lines, sizes):
        x = (WIDTH - tw) // 2
        if shadow:
            draw.text((x + 3, y + 3), ln, font=font, fill=(0, 0, 0))
        draw.text((x, y), ln, font=font, fill=fill)
        y += line_h


# ----------------------------------------------------------------------------
# Frame composition
# ----------------------------------------------------------------------------
def _compose_static_layers(
    bg: Image.Image,
    title: str,
    hook: str,
    body_lines: List[str],
    cta: str,
    brand: str,
) -> Dict[str, Any]:
    """Pre-measure and pre-wrap everything once; returns layout info reused per frame."""
    draw = ImageDraw.Draw(bg)

    f_brand = _load_font(46)
    f_hook = _load_font(82)
    f_body = _load_font(54)
    f_cta = _load_font(60)

    margin = 90
    max_w = WIDTH - 2 * margin

    hook_lines = _wrap(draw, hook or title or brand, f_hook, max_w)
    cta_lines = _wrap(draw, cta or "", f_cta, max_w)

    return {
        "f_brand": f_brand,
        "f_hook": f_hook,
        "f_body": f_body,
        "f_cta": f_cta,
        "hook_lines": hook_lines,
        "body_lines": body_lines,
        "cta_lines": cta_lines,
        "brand": brand,
        "max_w": max_w,
        "margin": margin,
    }


def _render_frame(bg: Image.Image, layout: Dict[str, Any], frame_idx: int) -> Image.Image:
    """Compose one frame with simple fades + a gold accent sweep."""
    img = bg.copy()
    draw = ImageDraw.Draw(img, "RGBA")

    t = frame_idx / max(TOTAL_FRAMES - 1, 1)  # 0..1

    # Top brand line (fade in over first 0.6s) — auto-fit so spaced caps never clip.
    brand_alpha = min(1.0, t / 0.10) if t < 0.12 else 1.0
    brand_txt = _spaced(layout["brand"].upper())
    bf = _fit_font(draw, brand_txt, base_size=46, max_w=WIDTH - 2 * layout["margin"], bold=True)
    bw, _ = _measure(draw, brand_txt, bf)
    bx = (WIDTH - bw) // 2
    draw.text((bx, 150), brand_txt, font=bf, fill=GOLD + (int(255 * brand_alpha),))

    # Thin gold divider that grows
    div_w = int((WIDTH - 2 * layout["margin"]) * min(1.0, t / 0.25))
    dx0 = (WIDTH - div_w) // 2
    draw.rectangle([dx0, 235, dx0 + div_w, 239], fill=GOLD_SOFT + (220,))

    # Hook (big) — fade/scale-ish via alpha, centered upper third
    hook_alpha = max(0.0, min(1.0, (t - 0.05) / 0.20))
    _draw_center_block(
        draw,
        layout["hook_lines"],
        layout["f_hook"],
        y_center=int(HEIGHT * 0.34),
        fill=CREAM + (int(255 * hook_alpha),) if len(CREAM) == 3 else CREAM,
        line_gap=14,
    )

    # Body lines reveal one-by-one across the middle of the clip
    body = layout["body_lines"]
    if body:
        reveal_start, reveal_end = 0.25, 0.80
        span = reveal_end - reveal_start
        per = span / max(len(body), 1)
        visible: List[str] = []
        for i, ln in enumerate(body):
            if t >= reveal_start + i * per:
                visible.append(ln)
        if visible:
            _draw_center_block(
                draw,
                visible,
                layout["f_body"],
                y_center=int(HEIGHT * 0.60),
                fill=MUTED,
                line_gap=22,
            )

    # Gold sweep accent line near CTA
    sweep_y = int(HEIGHT * 0.80)
    sweep_x = int(layout["margin"] + (WIDTH - 2 * layout["margin"]) * (0.5 + 0.5 * math.sin(t * math.pi)))
    draw.ellipse([sweep_x - 6, sweep_y - 6, sweep_x + 6, sweep_y + 6], fill=GOLD + (255,))

    # CTA (fade in last second)
    cta_alpha = max(0.0, min(1.0, (t - 0.70) / 0.20))
    if layout["cta_lines"]:
        _draw_center_block(
            draw,
            layout["cta_lines"],
            layout["f_cta"],
            y_center=int(HEIGHT * 0.86),
            fill=GOLD + (int(255 * cta_alpha),),
            line_gap=12,
        )

    # Subtle vignette
    _apply_vignette(img)
    return img.convert("RGB")


def _spaced(s: str) -> str:
    return " ".join(list(s))


def _apply_vignette(img: Image.Image) -> None:
    # cheap vignette: dark rounded border overlay
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    pad = 0
    for i, alpha in enumerate((40, 26, 14)):
        off = pad + i * 26
        od.rectangle([off, off, WIDTH - off, HEIGHT - off], outline=(0, 0, 0, alpha), width=26)
    img.alpha_composite(overlay) if img.mode == "RGBA" else img.paste(
        Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    )


# ----------------------------------------------------------------------------
# Plan -> text extraction
# ----------------------------------------------------------------------------
def _plan_text(plan: Dict[str, Any]) -> Dict[str, Any]:
    brand = plan.get("product_name") or plan.get("short_title") or "TREZZY"
    title = plan.get("title") or plan.get("topic") or "TREZZY"
    hook = plan.get("hook") or title
    cta = plan.get("cta") or "Свяжитесь с нами"

    # Body: prefer a couple of vibe tags / a trimmed script line
    body: List[str] = []
    vibe = plan.get("vibe_tags") or []
    if isinstance(vibe, list) and vibe:
        body.append(" · ".join(str(v) for v in vibe[:3]))
    script = plan.get("script") or ""
    if script:
        first = script.strip().split("\n")[0].strip()
        if first and first.lower() != hook.strip().lower():
            body.append(first[:80])
    return {"brand": str(brand), "title": str(title), "hook": str(hook), "cta": str(cta), "body": body[:3]}


# ----------------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------------
def render_fast(plan: Dict[str, Any], job_dir: Path) -> Dict[str, Any]:
    """Render a fast-mode MP4. Raises RuntimeError if ffmpeg is missing."""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg not found. Install it (winget install Gyan.FFmpeg) or "
            "`pip install imageio-ffmpeg`, or set FFMPEG_BINARY."
        )

    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)

    repo_root = Path(__file__).resolve().parents[2]
    output_dir = repo_root / "output"
    latest_dir = output_dir / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)

    txt = _plan_text(plan)
    bg = _vertical_gradient(BG_TOP, BG_BOTTOM)
    layout = _compose_static_layers(
        bg,
        title=txt["title"],
        hook=txt["hook"],
        body_lines=txt["body"],
        cta=txt["cta"],
        brand=txt["brand"],
    )

    frames_dir = Path(tempfile.mkdtemp(prefix="trezzy_frames_"))
    try:
        # Many frames are identical except for reveal/alpha steps; still cheap at 1080x1920/30fps/6s.
        for idx in range(TOTAL_FRAMES):
            frame = _render_frame(bg, layout, idx)
            frame.save(frames_dir / f"f_{idx:04d}.png", "PNG", compress_level=1)

        final_mp4 = job_dir / "final.mp4"
        cmd = [
            ffmpeg,
            "-y",
            "-framerate", str(FPS),
            "-i", str(frames_dir / "f_%04d.png"),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "veryfast",
            "-crf", "20",
            "-movflags", "+faststart",
            str(final_mp4),
        ]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode != 0 or not final_mp4.exists():
            err = proc.stderr.decode("utf-8", errors="replace")[-1200:]
            raise RuntimeError(f"ffmpeg failed (code {proc.returncode}): {err}")

        # Mirror to the canonical output locations.
        for dst in (latest_dir / "final.mp4", output_dir / "final.mp4"):
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(final_mp4, dst)
            except Exception:
                pass

        return {
            "output_path": str(final_mp4),
            "package_dir": str(job_dir),
            "duration_seconds": float(DURATION_SECONDS),
            "width": WIDTH,
            "height": HEIGHT,
            "fps": FPS,
            "renderer": "local_fast",
        }
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)


__all__ = ["render_fast", "WIDTH", "HEIGHT", "DURATION_SECONDS"]
