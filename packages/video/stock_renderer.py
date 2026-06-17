# -*- coding: utf-8 -*-
"""Realistic stock-footage renderer for TREZZY (render_mode="real").

Assembles a human-shot-looking vertical short from REAL Pexels clips instead of
a text-on-gradient slide:

  StockDirector → search queries → Pexels download → normalize each to 1080x1920
  → concat into one reel → burn the script as timed captions → optional music.

Looks like a person filmed it (because real people did), not like AI. Dependency
-light: ffmpeg (via imageio-ffmpeg) + PIL only. Reuses the clip engine's caption
renderer so subtitles match the rest of the factory.

Raises RuntimeError with a clear message when it can't proceed (no Pexels key, no
clips) so the dashboard shows an actionable error; the caller marks the job failed.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .local_renderer import _find_ffmpeg
from .clip_renderer import (_make_caption_png, video_duration, _run, _quality,
                            _verify, _final_extra_args)

WIDTH, HEIGHT, FPS = 1080, 1920, 30
DEFAULT_CLIPS = 5
DEFAULT_SECONDS_PER_CLIP = 3.5
MUSIC_EXTS = (".mp3", ".m4a", ".aac", ".wav", ".ogg")


def _norm_vf() -> str:
    # Fill 9:16 by covering then center-cropping; uniform fps for clean concat.
    # Лёгкий CAS возвращает резкость после ресайза стоковых кадров.
    return (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},cas=0.3,setsar=1,fps={FPS}")


def _caption_lines(plan: Dict[str, Any]) -> List[str]:
    """hook + each script sentence + cta — the spoken/on-screen message."""
    hook = (plan.get("hook") or "").strip()
    script = (plan.get("script") or "").strip()
    cta = (plan.get("cta") or "").strip()
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", script) if p.strip()]
    lines: List[str] = []
    if hook:
        lines.append(hook)
    for p in parts:
        if p.lower() != hook.lower():
            lines.append(p)
    if cta and cta.lower() not in (l.lower() for l in lines):
        lines.append(cta)
    return lines[:8] or [plan.get("topic") or "TREZZY"]


def _find_music(repo_root: Path) -> Optional[Path]:
    mdir = repo_root / "assets" / "music"
    if not mdir.exists():
        return None
    for p in sorted(mdir.iterdir()):
        if p.is_file() and p.suffix.lower() in MUSIC_EXTS:
            return p
    return None


def _llm_creds(settings: Dict[str, Any]):
    if settings.get("anthropic_api_key"):
        return "anthropic", settings["anthropic_api_key"]
    if settings.get("openai_api_key"):
        return "openai", settings["openai_api_key"]
    if settings.get("groq_api_key"):
        return "groq", settings["groq_api_key"]
    return None, None


def _fetch_clips(queries: List[str], api_key: str, want: int, workdir: Path) -> List[Path]:
    """Search + download up to `want` portrait clips across the queries."""
    from .stock_client import search_portrait_clips, download

    got: List[Path] = []
    seen_links = set()
    for q in queries:
        if len(got) >= want:
            break
        for clip in search_portrait_clips(q, api_key, per_page=6):
            if len(got) >= want:
                break
            link = clip.get("link")
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            dest = workdir / f"raw_{len(got):02d}.mp4"
            if download(link, dest):
                got.append(dest)
                break  # got one good clip → next query (footage variety)
            # download failed → try the next candidate within THIS query
    # If queries were exhausted but we still need more, take extra clips from the
    # first query's remaining results.
    if len(got) < want and queries:
        for clip in search_portrait_clips(queries[0], api_key, per_page=max(want * 2, 10)):
            if len(got) >= want:
                break
            link = clip.get("link")
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            dest = workdir / f"raw_{len(got):02d}.mp4"
            if download(link, dest):
                got.append(dest)
    return got


def render_stock(plan: Dict[str, Any], job_dir: str | Path, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Render a realistic short from Pexels stock footage. Raises RuntimeError on
    a missing key / no footage (clear, actionable message)."""
    settings = settings or {}
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found (needed to assemble stock video).")

    pexels_key = settings.get("pexels_api_key") or ""
    if not pexels_key:
        raise RuntimeError(
            "Нет ключа Pexels. Получи бесплатный на pexels.com/api и добавь "
            "pexels_api_key в Настройки — для реалистичного видео из живых кадров."
        )

    n_clips = int(settings.get("stock_clip_count") or DEFAULT_CLIPS)
    n_clips = max(2, min(n_clips, 8))
    per = float(settings.get("stock_seconds_per_clip") or DEFAULT_SECONDS_PER_CLIP)
    q = _quality(settings)

    repo_root = Path(__file__).resolve().parents[2]
    output_dir = repo_root / "output"
    latest_dir = output_dir / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)

    # 1) Search queries (StockDirector: LLM → local fallback).
    from packages.agents.stock_director import StockDirectorAgent
    from packages.agents.base import AgentContext

    prov, key = _llm_creds(settings)
    ctx = AgentContext(topic=plan.get("topic") or "TREZZY",
                       product_name=plan.get("product_name") or "TREZZY", format="real")
    director = StockDirectorAgent(llm_provider=prov, llm_key=key)
    queries = director.run(
        ctx, hook=plan.get("hook"), script=plan.get("script"),
        vibe_tags=plan.get("vibe_tags"), count=max(n_clips, 5),
    ).get("queries", [])
    if not queries:
        raise RuntimeError("StockDirector вернул пустой список запросов.")

    workdir = Path(tempfile.mkdtemp(prefix="trezzy_stock_"))
    try:
        # 2) Fetch real clips.
        raws = _fetch_clips(queries, pexels_key, n_clips, workdir)
        if not raws:
            raise RuntimeError(
                "Pexels не вернул подходящих вертикальных кадров. Проверь ключ/сеть "
                "или попробуй другую тему."
            )

        # 3) Normalize each clip to 1080x1920 / 30fps / no audio.
        norms: List[Path] = []
        for i, raw in enumerate(raws):
            norm = workdir / f"norm_{i:02d}.mp4"
            ok, err = _run([
                ffmpeg, "-y", "-i", str(raw), "-t", f"{per:.2f}", "-an",
                "-vf", _norm_vf(),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                "-crf", q["inter_crf"], "-movflags", "+faststart", str(norm),
            ])
            if ok and norm.exists():
                norms.append(norm)
            else:
                print(f"[stock] normalize failed for clip {i}:", err[:200])
        if not norms:
            raise RuntimeError("Не удалось обработать ни одного скачанного клипа.")

        # 4) Concat into one reel (identical params → stream copy).
        list_file = workdir / "concat.txt"
        list_file.write_text(
            "".join(f"file '{p.as_posix()}'\n" for p in norms), encoding="utf-8"
        )
        reel = workdir / "reel.mp4"
        ok, err = _run([
            ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", "-movflags", "+faststart", str(reel),
        ])
        if not ok or not reel.exists():
            # Fallback: re-encode concat (handles any stray param mismatch).
            ok, err = _run([
                ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                "-crf", "21", str(reel),
            ])
            if not ok or not reel.exists():
                raise RuntimeError(f"Склейка клипов не удалась: {err[:200]}")

        total = video_duration(reel) or (per * len(norms))

        # 5) Build timed captions (hook + script + cta) across the reel.
        lines = _caption_lines(plan)
        slice_s = total / max(len(lines), 1)
        caps: List[Dict[str, Any]] = []
        for i, text in enumerate(lines):
            a = round(i * slice_s, 2)
            b = round(min(total, (i + 1) * slice_s) - 0.05, 2)
            if b - a < 0.4:
                continue
            png = workdir / f"cap_{i:02d}.png"
            pos = _make_caption_png(text, png)   # cropped PNG + its overlay position
            if pos:
                caps.append({"a": a, "b": b, "png": png, "x": pos[0], "y": pos[1]})

        music = _find_music(repo_root)

        # 6) Final assembly: overlay captions (+ optional music) onto the reel.
        final_mp4 = job_dir / "final.mp4"
        cmd: List[str] = [ffmpeg, "-y", "-i", str(reel)]
        if music:
            cmd += ["-stream_loop", "-1", "-i", str(music)]  # loop music to cover the reel
        for c in caps:
            cmd += ["-loop", "1", "-framerate", "2", "-i", str(c["png"])]  # 2fps decode — speed

        if caps:
            png_start = 2 if music else 1
            chain: List[str] = []
            cur = "0:v"
            for i, c in enumerate(caps):
                nxt = f"v{i+1}"
                chain.append(
                    f"[{cur}][{png_start + i}:v]overlay={c['x']}:{c['y']}:"
                    f"enable='between(t,{c['a']:.2f},{c['b']:.2f})'[{nxt}]"
                )
                cur = nxt
            cmd += ["-filter_complex", ";".join(chain), "-map", f"[{cur}]"]
        else:
            cmd += ["-map", "0:v"]

        if music:
            cmd += ["-map", "1:a", "-c:a", "aac", "-b:a", q["abitrate"]]
        # Hard-bound the output length with -t: looped image/music inputs are infinite
        # and -shortest is unreliable with filter_complex (was hanging). -t guarantees
        # the overlay pass terminates at the reel duration.
        cmd += ["-t", f"{total:.3f}", "-shortest",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", q["preset"],
                "-crf", q["crf"]] + _final_extra_args(q) + [
                "-movflags", "+faststart", str(final_mp4)]

        ok, err = _run(cmd)
        if not ok or not final_mp4.exists():
            print("[stock] caption/music pass failed, using plain reel:", err[:200])
            shutil.copyfile(reel, final_mp4)
        if not _verify(ffmpeg, final_mp4):
            print("[stock] final failed decode verification, using plain reel")
            shutil.copyfile(reel, final_mp4)

        # Mirror to canonical locations (dashboard preview).
        for dst in (latest_dir / "final.mp4", output_dir / "final.mp4"):
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(final_mp4, dst)
            except Exception:
                pass

        return {
            "output_path": str(final_mp4),
            "package_dir": str(job_dir),
            "duration_seconds": round(total, 1),
            "renderer": "stock_real",
            "clip_count": len(norms),
            "queries": queries[:n_clips],
            "music": bool(music),
            "captions": len(caps),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


__all__ = ["render_stock"]
