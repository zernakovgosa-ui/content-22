# -*- coding: utf-8 -*-
"""Clip engine for TREZZY (render_mode="clip").

Takes a long LOCAL video + a list of "moments" chosen by ClipAgent and cuts each
into a vertical 1080x1920 short with face-aware framing and burned-in subtitles.

Per moment:
  1. Accurate+fast cut  — input-seek to a keyframe before START, output-seek the
     remainder (clean 0-based timeline → captions align perfectly).
  2. 9:16 reframe       — crop a vertical window centered on the detected face
     (falls back to center crop), then scale to 1080x1920.
  3. Burn captions      — render each transcript segment as a transparent PNG
     (Cyrillic via the local_renderer fonts) and overlay it during its time
     window. Avoids any libass dependency in the bundled ffmpeg.

final.mp4 = the first (best) clip, so the existing dashboard preview and
output/latest mirror keep working unchanged. All clips + a clips.json manifest +
per-clip .srt/caption/hashtags land in the job dir.

Dependency-light + synchronous (runs inside the API thread executor). NEVER let a
single bad moment abort the whole job — failures are skipped and logged.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw

from .local_renderer import _find_ffmpeg, _load_font, _wrap, _measure
from .face_crop import face_center_x

WIDTH = 1080
HEIGHT = 1920
TARGET_AR = WIDTH / HEIGHT  # 0.5625


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
def _even(n: float) -> int:
    n = int(round(n))
    return n - (n % 2)


def _srt_tc(t: float) -> str:
    t = max(0.0, float(t))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _run(cmd: List[str], timeout: int = 600) -> Tuple[bool, str]:
    # Hard timeout so a stuck ffmpeg can never freeze the render thread (and the API).
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"ffmpeg timed out after {timeout}s"
    except Exception as e:
        return False, f"ffmpeg failed to start: {e}"
    if proc.returncode != 0:
        return False, proc.stderr.decode("utf-8", errors="replace")[-800:]
    return True, ""


def _quality(settings: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Encoder profile. 'max' (default) = slow + high quality; 'fast' = quick.

    inter_crf is the near-lossless intermediate (pass 1) used when captions follow,
    so the only real compression is the final pass.
    """
    mode = ((settings or {}).get("render_quality") or "max").lower()
    if mode == "fast":
        return {"preset": "veryfast", "crf": "21", "inter_crf": "18", "abitrate": "128k",
                "tune": ""}
    # Quality is set by CRF, not the preset. crf15 + tune film даёт YouTube
    # больше бит и кино-фактуру на входе — после его пережатия картинка чище.
    return {"preset": "veryfast", "crf": "15", "inter_crf": "12", "abitrate": "192k",
            "tune": "film"}


def _final_extra_args(q: Dict[str, str]) -> List[str]:
    """Аргументы ТОЛЬКО финального энкода: psy-тюнинг + явные теги BT.709/tv —
    без них YouTube/плееры могут трактовать цвет как BT.601 (плывут тона кожи)."""
    args: List[str] = []
    if q.get("tune"):
        args += ["-tune", q["tune"]]
    args += ["-colorspace", "bt709", "-color_primaries", "bt709",
             "-color_trc", "bt709", "-color_range", "tv"]
    return args


def _verify(ffmpeg: str, path: Path) -> bool:
    """True if ffmpeg fully decodes `path` (catches truncated / broken output)."""
    try:
        if not Path(path).exists() or Path(path).stat().st_size < 2048:
            return False
        p = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return p.returncode == 0
    except Exception:
        return False


def video_duration(source_path: str | Path) -> Optional[float]:
    """Best-effort source duration in seconds (parsed from ffmpeg stderr).

    Uses ffmpeg only (no ffprobe dependency). Returns None if unknown.
    """
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return None
    try:
        proc = subprocess.run([ffmpeg, "-i", str(source_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    except Exception:
        return None
    err = proc.stderr.decode("utf-8", errors="replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", err)
    if not m:
        return None
    try:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Sampling + framing geometry
# ----------------------------------------------------------------------------
def _sample_frames(ffmpeg: str, src: Path, start: float, end: float, workdir: Path, n: int = 5) -> List[Path]:
    """Grab n evenly-spaced frames from [start,end] for size + face detection."""
    paths: List[Path] = []
    dur = max(0.1, end - start)
    for i in range(n):
        t = start + dur * (i + 1) / (n + 1)
        p = workdir / f"sample_{i}.png"
        ok, _ = _run([ffmpeg, "-y", "-ss", f"{t:.3f}", "-i", str(src),
                      "-frames:v", "1", "-q:v", "3", str(p)])
        if ok and p.exists():
            paths.append(p)
    return paths


def _frame_size(frame_path: Path) -> Optional[Tuple[int, int]]:
    try:
        with Image.open(frame_path) as im:
            return im.size  # (w, h)
    except Exception:
        return None


def _crop_geometry(src_w: int, src_h: int, face_nx: Optional[float]) -> Tuple[int, int, int, int]:
    """Return (crop_w, crop_h, crop_x, crop_y) for a 9:16 window."""
    if src_w / src_h >= TARGET_AR:
        # Source is wider than 9:16 → full height, crop width toward the face.
        ch = _even(src_h)
        cw = _even(min(src_w, src_h * TARGET_AR))
        nx = face_nx if face_nx is not None else 0.5
        cx = _even(min(max(nx * src_w - cw / 2.0, 0), src_w - cw))
        cy = 0
    else:
        # Source is taller/narrower than 9:16 → full width, center crop height.
        cw = _even(src_w)
        ch = _even(min(src_h, src_w / TARGET_AR))
        cx = 0
        cy = _even(min(max((src_h - ch) / 2.0, 0), src_h - ch))
    return cw, ch, cx, cy


def _crop_geometry_wide(src_w: int, src_h: int, face_nx: Optional[float],
                        widen: float = 1.35) -> Tuple[int, int, int, int]:
    """A WIDER face-centered crop for the 'wide' framing: the subject appears
    ~25% smaller (owner: "масштаб чутка поменьше"), the 9:16 frame is filled
    with a blurred backdrop. Falls back to the fill crop when the source can't
    get any wider (portrait/square sources)."""
    cw, ch, cx, cy = _crop_geometry(src_w, src_h, face_nx)
    w2 = _even(min(src_w, cw * widen))
    if w2 <= cw + 8:                      # can't widen → same as fill
        return cw, ch, cx, cy
    nx = face_nx if face_nx is not None else 0.5
    x2 = _even(min(max(nx * src_w - w2 / 2.0, 0), src_w - w2))
    return w2, ch, x2, cy


# ----------------------------------------------------------------------------
# Captions
# ----------------------------------------------------------------------------
def _clip_caption_lines(transcript: Dict[str, Any], start: float, end: float, max_lines: int = 40) -> List[Dict]:
    """Transcript segments overlapping [start,end], in clip-relative time."""
    caps: List[Dict] = []
    for seg in (transcript or {}).get("segments") or []:
        try:
            s = max(start, float(seg["start"]))
            e = min(end, float(seg["end"]))
        except Exception:
            continue
        text = (seg.get("text") or "").strip()
        if not text or (e - s) < 0.2:
            continue
        caps.append({"a": round(s - start, 2), "b": round(e - start, 2), "text": text})
        if len(caps) >= max_lines:
            break
    return caps


def _word_chunks(transcript: Dict[str, Any], start: float, end: float,
                 max_words: int = 4, max_dur: float = 1.6, max_chunks: int = 120) -> List[Dict]:
    """Group transcript WORDS in [start,end] into short punchy caption chunks.

    Each chunk: {"a","b","text","words":[{"a","b","t"}...]} in CLIP-relative time —
    the per-word times drive the karaoke highlight. [] if no word-level timings."""
    words = (transcript or {}).get("words") or []
    chunks: List[Dict] = []
    cur: List[Dict] = []
    cur_a: Optional[float] = None
    cur_b: float = 0.0

    def _flush() -> None:
        nonlocal cur, cur_a
        if cur and cur_a is not None:
            chunks.append({
                "a": round(cur_a - start, 2), "b": round(cur_b - start, 2),
                "text": " ".join(w["t"] for w in cur), "words": cur,
            })
        cur, cur_a = [], None

    for w in words:
        try:
            ws = float(w["start"]); we = float(w["end"])
        except Exception:
            continue
        txt = (w.get("word") or w.get("text") or "").strip()
        if not txt or we <= start or ws >= end:
            continue
        ws, we = max(ws, start), min(we, end)
        if cur_a is None:
            cur_a = ws
        cur.append({"a": round(ws - start, 2), "b": round(we - start, 2), "t": txt})
        cur_b = we
        if len(cur) >= max_words or (cur_b - cur_a) >= max_dur:
            _flush()
        if len(chunks) >= max_chunks:
            break
    _flush()
    chunks = [c for c in chunks if c["b"] - c["a"] >= 0.2]
    # Whisper иногда даёт пересекающиеся тайминги слов → два блока субтитров
    # видны ОДНОВРЕМЕННО и накладываются. Нормализуем: чанк держится до начала
    # следующего (плюс короткое удержание на паузах), но никогда не пересекается.
    for i in range(len(chunks) - 1):
        nxt_a = chunks[i + 1]["a"]
        chunks[i]["b"] = round(max(chunks[i]["a"] + 0.2,
                                   min(nxt_a - 0.02, chunks[i]["b"] + 0.35)), 2)
        # последнее слово чанка не должно светиться дольше самого чанка
        if chunks[i].get("words"):
            chunks[i]["words"][-1]["b"] = min(chunks[i]["words"][-1]["b"], chunks[i]["b"])
    if chunks:
        chunks[-1]["b"] = round(chunks[-1]["b"] + 0.3, 2)
    # Жёсткая страховка от патологии Whisper (одинаковые тайминги слов):
    # следующий чанк начинается строго ПОСЛЕ конца предыдущего, пустые — долой.
    fixed: List[Dict] = []
    prev_b = -1.0
    for c in chunks:
        c["a"] = round(max(c["a"], prev_b + 0.01), 2)
        if c["b"] - c["a"] >= 0.2:
            fixed.append(c)
            prev_b = c["b"]
    return fixed


def _make_caption_png(text: str, out_path: Path) -> Optional[Tuple[int, int]]:
    """Render a punchy caption as a CROPPED transparent PNG.

    Big, bold, centered, thick black outline (modern Reels/TikTok look).
    Returns the (x, y) the cropped PNG must be overlaid at, or None on failure.
    """
    try:
        text = (text or "").strip()
        if not text:
            return None
        img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        max_w = WIDTH - 150

        # Auto-fit: start big, shrink until it fits in <= 2 lines.
        size = 72
        font = _load_font(size)
        lines = _wrap(draw, text, font, max_w)
        while len(lines) > 2 and size > 44:
            size -= 6
            font = _load_font(size)
            lines = _wrap(draw, text, font, max_w)
        lines = lines[:2]
        if not lines:
            return None
        stroke = max(3, size // 14)

        def _wh(ln: str):
            b = draw.textbbox((0, 0), ln, font=font, stroke_width=stroke)
            return b[2] - b[0], b[3] - b[1]

        dims = [_wh(ln) for ln in lines]
        line_h = max((h for _, h in dims), default=size) + 16
        block_h = line_h * len(lines)
        y = int(HEIGHT * 0.72) - block_h // 2
        for ln, (w, _) in zip(lines, dims):
            x = (WIDTH - w) // 2
            draw.text((x + 3, y + 4), ln, font=font, fill=(0, 0, 0, 110),
                      stroke_width=stroke, stroke_fill=(0, 0, 0, 110))
            draw.text((x, y), ln, font=font, fill=(255, 255, 255, 255),
                      stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
            y += line_h

        cropped, ox, oy = _crop_to_content(img)
        cropped.save(out_path, "PNG")
        return ox, oy
    except Exception:
        return None


GOLD = (255, 209, 84, 255)        # active-word highlight (TREZZY-warm gold)
WHITE = (255, 255, 255, 255)
BLACK = (0, 0, 0, 255)
MAX_OVERLAYS_PER_PASS = 180       # filter-граф идёт через -filter_complex_script,
                                  # cmd растят только "-loop 1 -i png" (~70 байт) —
                                  # 180 входов ≈ 13КБ, лимит Windows 32КБ. Больше
                                  # входов за проход = меньше переэнкодов-поколений.
OVERLAY_INPUT_FPS = "2"           # looped PNGs decode at 2fps, not 30 (huge speedup)


def _crop_to_content(img: Image.Image, pad: int = 12) -> Tuple[Image.Image, int, int]:
    """Crop a transparent canvas down to its drawn content (+pad).

    Full-frame 1080x1920 caption PNGs make ffmpeg's overlay pass pathologically
    slow (every input is re-decoded constantly). Cropping to the text bbox cuts
    decoded pixels ~50-100x; the (x, y) offset repositions it via overlay=x:y.
    """
    box = img.getbbox()
    if not box:
        return img, 0, 0
    x0 = max(0, box[0] - pad)
    y0 = max(0, box[1] - pad)
    x1 = min(img.width, box[2] + pad)
    y1 = min(img.height, box[3] + pad)
    return img.crop((x0, y0, x1, y1)), x0, y0


def _layout_caption(draw: "ImageDraw.ImageDraw", words: List[str], max_w: int):
    """Autosize + wrap a word list into <=2 centered lines.

    Returns (font, stroke, lines, space_w) where lines = [[(word, w_px), ...], ...].
    """
    size = 72   # компактнее: читается чётко, но не загораживает кадр
    while True:
        font = _load_font(size)
        stroke = max(3, size // 14)   # тоньше обводка — аккуратнее вид

        def _w(t: str) -> int:
            b = draw.textbbox((0, 0), t, font=font, stroke_width=stroke)
            return b[2] - b[0]

        space_w = max(int(draw.textlength(" ", font=font)), size // 4)
        lines: List[List[Tuple[str, int]]] = []
        cur: List[Tuple[str, int]] = []
        cur_w = 0
        for w in words:
            wpx = _w(w)
            add = wpx if not cur else wpx + space_w
            if cur and cur_w + add > max_w:
                lines.append(cur)
                cur, cur_w = [(w, wpx)], wpx
            else:
                cur.append((w, wpx))
                cur_w += add
        if cur:
            lines.append(cur)
        if len(lines) <= 2 or size <= 48:
            return font, stroke, lines, space_w
        size -= 6


def _render_caption_state(texts: List[str], highlight_idx: Optional[int],
                          out_path: Path) -> bool:
    """ПОЛНОКАДРОВЫЙ (1080x1920) PNG-кадр субтитров: все слова белые, активное
    (highlight_idx) — золотое. Без кропа: такие кадры идут в ffconcat-ленту,
    которая накладывается ОДНИМ overlay — каждый PNG декодируется один раз."""
    try:
        if not texts:
            return False
        probe = Image.new("RGBA", (8, 8))
        pd = ImageDraw.Draw(probe)
        font, stroke, lines, space_w = _layout_caption(pd, texts, WIDTH - 150)
        bb = pd.textbbox((0, 0), "Ауj", font=font, stroke_width=stroke)
        line_h = (bb[3] - bb[1]) + 18
        block_h = line_h * len(lines)
        y0 = int(HEIGHT * 0.70) - block_h // 2

        img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        idx = 0
        for li, line in enumerate(lines):
            lw = sum(w for _, w in line) + space_w * (len(line) - 1)
            x = (WIDTH - lw) // 2
            y = y0 + li * line_h
            for (t, wpx) in line:
                color = GOLD if idx == highlight_idx else WHITE
                d.text((x, y), t, font=font, fill=color, stroke_width=stroke, stroke_fill=BLACK)
                x += wpx + space_w
                idx += 1
        img.save(out_path, "PNG")
        return True
    except Exception:
        return False


def _build_caption_track(caps: List[Dict], dur: float, workdir: Path) -> Optional[Path]:
    """Собрать .ffconcat-ленту субтитров на весь клип [0..dur].

    Караоке-чанк → по кадру на каждое слово (текст чанка, активное слово золотом),
    обычный чанк → один кадр; паузы → общий прозрачный кадр. Лента накладывается
    одним overlay → фильтр-граф плоский, наложения исключены по построению."""
    try:
        gap = workdir / "cap_gap.png"
        Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0)).save(gap, "PNG")

        entries: List[Tuple[Path, float]] = []   # (png, длительность)
        cursor = 0.0
        n_png = 0
        for c in caps:
            a, b = float(c["a"]), float(c["b"])
            if a > cursor + 0.01:
                entries.append((gap, a - cursor))
            words = c.get("words") or []
            if words:
                texts = [w["t"] for w in words]
                for wi, w in enumerate(words):
                    wa = max(float(w["a"]), a)
                    wb = float(words[wi + 1]["a"]) if wi + 1 < len(words) else b
                    wb = min(wb, b)
                    if wb - wa < 0.05:
                        continue
                    p = workdir / f"st_{n_png:04d}.png"
                    if _render_caption_state(texts, wi, p):
                        entries.append((p, wb - wa))
                        n_png += 1
            else:
                p = workdir / f"st_{n_png:04d}.png"
                if _render_caption_state(str(c.get("text") or "").split() or ["…"], None, p):
                    entries.append((p, b - a))
                    n_png += 1
            cursor = b
        if cursor < dur:
            entries.append((gap, dur - cursor))
        if not any(p != gap for p, _ in entries):
            return None

        lines = ["ffconcat version 1.0"]
        for p, d in entries:
            lines.append(f"file '{p.as_posix()}'")
            lines.append(f"duration {max(d, 0.04):.3f}")
        # повторяем последний файл без duration — требование concat-демаксера
        lines.append(f"file '{entries[-1][0].as_posix()}'")
        track = workdir / "captions.ffconcat"
        track.write_text("\n".join(lines), encoding="utf-8")
        return track
    except Exception as e:
        print("[clip] caption track build failed:", repr(e))
        return None


def _make_karaoke_pngs(words: List[Dict], workdir: Path, prefix: str
                       ) -> Tuple[Optional[Tuple[Path, int, int]], List[Tuple[Path, int, int, int]]]:
    """Karaoke caption set for one chunk: a base PNG (all words white) + one PNG
    per word with ONLY that word in gold. All PNGs are CROPPED to content, so
    each carries its overlay position.

    Returns ((base_png, x, y), [(word_png, word_index, x, y), ...]); (None, []) on failure.
    """
    try:
        texts = [w["t"] for w in words]
        if not texts:
            return None, []
        probe = Image.new("RGBA", (8, 8))
        draw = ImageDraw.Draw(probe)
        font, stroke, lines, space_w = _layout_caption(draw, texts, WIDTH - 150)

        bb = draw.textbbox((0, 0), "Ауj", font=font, stroke_width=stroke)
        line_h = (bb[3] - bb[1]) + 16
        block_h = line_h * len(lines)
        y0 = int(HEIGHT * 0.72) - block_h // 2

        # Per-word absolute positions (parallel to `words` order).
        pos: List[Tuple[int, int]] = []
        for li, line in enumerate(lines):
            lw = sum(w for _, w in line) + space_w * (len(line) - 1)
            x = (WIDTH - lw) // 2
            y = y0 + li * line_h
            for (_, wpx) in line:
                pos.append((x, y))
                x += wpx + space_w

        base = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        bd = ImageDraw.Draw(base)
        for (x, y), t in zip(pos, texts):
            # мягкая тень → текст «отлипает» от фона, выглядит дороже
            bd.text((x + 3, y + 4), t, font=font, fill=(0, 0, 0, 110),
                    stroke_width=stroke, stroke_fill=(0, 0, 0, 110))
            bd.text((x, y), t, font=font, fill=WHITE, stroke_width=stroke, stroke_fill=BLACK)
        base_path = workdir / f"{prefix}_base.png"
        base_c, bx, by = _crop_to_content(base)
        base_c.save(base_path, "PNG")

        word_pngs: List[Tuple[Path, int, int, int]] = []
        for idx, ((x, y), t) in enumerate(zip(pos, texts)):
            im = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            d.text((x + 3, y + 4), t, font=font, fill=(0, 0, 0, 110),
                   stroke_width=stroke, stroke_fill=(0, 0, 0, 110))
            d.text((x, y), t, font=font, fill=GOLD, stroke_width=stroke, stroke_fill=BLACK)
            p = workdir / f"{prefix}_w{idx:02d}.png"
            im_c, wx, wy = _crop_to_content(im)
            im_c.save(p, "PNG")
            word_pngs.append((p, idx, wx, wy))
        return (base_path, bx, by), word_pngs
    except Exception:
        return None, []


def _make_title_png(title: str, out_path: Path) -> Optional[Tuple[int, int]]:
    """Hook headline shown for the first ~2.5s (top of frame, bold, outlined).
    Returns the overlay (x, y) for the cropped PNG, or None."""
    try:
        title = (title or "").strip()
        if not title:
            return None
        img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        font, stroke, lines, space_w = _layout_caption(draw, title.split(), WIDTH - 170)
        bb = draw.textbbox((0, 0), "Ауj", font=font, stroke_width=stroke)
        line_h = (bb[3] - bb[1]) + 14
        y = int(HEIGHT * 0.115) - (line_h * len(lines)) // 2
        for line in lines:
            lw = sum(w for _, w in line) + space_w * (len(line) - 1)
            x = (WIDTH - lw) // 2
            for (t, wpx) in line:
                draw.text((x + 3, y + 4), t, font=font, fill=(0, 0, 0, 110),
                          stroke_width=stroke, stroke_fill=(0, 0, 0, 110))
                draw.text((x, y), t, font=font, fill=GOLD, stroke_width=stroke, stroke_fill=BLACK)
                x += wpx + space_w
            y += line_h
        cropped, ox, oy = _crop_to_content(img)
        cropped.save(out_path, "PNG")
        return ox, oy
    except Exception:
        return None


def _overlay_batched(ffmpeg: str, video_in: Path, overlays: List[Tuple[Path, float, float, int, int]],
                     out_path: Path, q: Dict[str, str], workdir: Path, dur: float) -> Tuple[bool, str]:
    """Overlay many timed PNGs (png, a, b, x, y) onto video_in → out_path, in
    passes of MAX_OVERLAYS_PER_PASS (so 300+ karaoke words can't overflow the
    command line). PNGs are content-cropped and decoded at OVERLAY_INPUT_FPS —
    without that, ffmpeg re-decodes every full-frame PNG ~30x/sec and a 3-clip
    job takes 15+ minutes. Intermediate passes are near-lossless; only the final
    pass really compresses. Filter graph via -filter_complex_script."""
    if not overlays:
        shutil.copyfile(video_in, out_path)
        return out_path.exists(), ""

    batches = [overlays[i:i + MAX_OVERLAYS_PER_PASS]
               for i in range(0, len(overlays), MAX_OVERLAYS_PER_PASS)]
    cur_in = video_in
    for bi, batch in enumerate(batches):
        if len(batches) > 1:
            print(f"[clip]    оверлеи: проход {bi + 1}/{len(batches)} ({len(batch)} слоёв)...")
        last = bi == len(batches) - 1
        out = out_path if last else workdir / f"ovpass_{bi}.mp4"
        cmd: List[str] = [ffmpeg, "-y", "-i", str(cur_in)]
        for png, _, _, _, _ in batch:
            cmd += ["-loop", "1", "-framerate", OVERLAY_INPUT_FPS, "-i", str(png)]
        chain: List[str] = []
        cur = "0:v"
        for k, (_, a, b, x, y) in enumerate(batch, start=1):
            nxt = f"v{k}"
            chain.append(f"[{cur}][{k}:v]overlay={x}:{y}:enable='between(t,{a:.2f},{b:.2f})'[{nxt}]")
            cur = nxt
        script = workdir / f"fcs_{bi}.txt"
        script.write_text(";".join(chain), encoding="utf-8")
        # Промежуточные проходы: crf8 = визуально без потерь, но в ~40 раз меньше
        # лослесса по объёму — иначе гигабайтные temp-файлы душат диск (владелец
        # жаловался «слишком долго режется»). 1-2 поколения crf8 деградации не дают.
        enc = (["-preset", q["preset"], "-crf", q["crf"]] + _final_extra_args(q)) if last \
            else ["-preset", "veryfast", "-crf", "8"]
        cmd += [
            "-filter_complex_script", str(script),
            "-map", f"[{cur}]", "-map", "0:a?", "-c:a", "copy",
            "-c:v", "libx264", "-pix_fmt", "yuv420p"] + enc + [
            "-t", f"{dur:.3f}", "-movflags", "+faststart", "-shortest", str(out),
        ]
        ok, err = _run(cmd, timeout=1800)
        if not ok or not Path(out).exists():
            return False, err
        cur_in = out
    return True, ""


def _write_srt(caps: List[Dict], path: Path) -> None:
    try:
        lines: List[str] = []
        for i, c in enumerate(caps, start=1):
            lines.append(str(i))
            lines.append(f"{_srt_tc(c['a'])} --> {_srt_tc(c['b'])}")
            lines.append(c["text"])
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Per-clip render — ОДИН проход: фильтры + ffconcat-лента субтитров + один энкод.
# Раньше было 2+ прохода и до ~200 цепочных overlay-фильтров (каждый кадр шёл
# через все) — рендер полз. Теперь субтитры — одна «слайд-лента» PNG с точными
# длительностями, наложенная ОДНИМ overlay; промежуточных файлов нет вообще.
# ----------------------------------------------------------------------------
def _render_one_clip(
    ffmpeg: str, src: Path, start: float, dur: float, caps: List[Dict],
    framing: Dict[str, Any], workdir: Path, out_path: Path,
    q: Dict[str, str], hook_title: str = "",
) -> bool:
    cw, ch, cx, cy = framing["fill"]

    enc_args = ["-preset", q["preset"], "-crf", q["crf"]] + _final_extra_args(q)
    # ОДИН точный input-seek по ИСХОДНИКУ. Раньше был двухступенчатый seek
    # (-ss ifast -i src -ss finep), но при добавлении второго входа -ss finep
    # «прилипал» к concat-ленте субтитров (это input-опция СЛЕДУЮЩЕГО -i) → видео
    # уезжало назад, субтитры вперёд, рассинхрон ~2-4с. Современный ffmpeg делает
    # input -ss точным (декодирует от ближайшего кейфрейма до точки) → и быстро,
    # и кадр 0 видео = ровно start, поэтому совпадает с лентой (её время 0 = start).
    cmd = [ffmpeg, "-y", "-ss", f"{max(0.0, start):.3f}", "-i", str(src)]

    # Лента субтитров (если есть) — вторым входом через concat-демаксер. БЕЗ seek:
    # её таймлайн уже clip-relative (0 = начало клипа).
    track = _build_caption_track(caps, dur, workdir) if caps else None
    if track is not None:
        cmd += ["-f", "concat", "-safe", "0", "-i", str(track)]

    # Качество апскейла: лёгкий денойз ДО scale (на малом кропе почти бесплатен,
    # чтобы lanczos+cas не усиливали зерно), полные chroma-флаги у swscale,
    # CAS-резкость по степени растяжки.
    SCALE_FLAGS = "lanczos+accurate_rnd+full_chroma_int+full_chroma_inp"
    w2, h2, x2, y2 = framing.get("wide", framing["fill"])
    if framing.get("mode") == "wide" and w2 > cw + 8:
        up_fg = WIDTH / max(w2, 1)
        cas_fg = 0.6 if up_fg >= 1.8 else (0.5 if up_fg >= 1.2 else 0.3)
        # Блюр-фон считаем на уменьшенной картинке — в ~4 раза дешевле, виду не вредит.
        fc = (
            f"[0:v]split=2[bgs][fgs];"
            f"[bgs]crop={cw}:{ch}:{cx}:{cy},scale=270:480,boxblur=10:2,"
            f"scale={WIDTH}:{HEIGHT}:flags=bilinear,setsar=1[bg];"
            f"[fgs]crop={w2}:{h2}:{x2}:{y2},hqdn3d=1.5:1.5:6:6,"
            f"scale={WIDTH}:-2:flags={SCALE_FLAGS},cas={cas_fg},setsar=1[fg];"
            f"[bg][fg]overlay=0:(main_h-overlay_h)/2[vbase]"
        )
    else:
        up = WIDTH / max(cw, 1)
        cas = 0.6 if up >= 1.8 else (0.5 if up >= 1.2 else 0.3)
        fc = (f"[0:v]crop={cw}:{ch}:{cx}:{cy},hqdn3d=1.5:1.5:6:6,"
              f"scale={WIDTH}:{HEIGHT}:flags={SCALE_FLAGS},cas={cas},setsar=1[vbase]")

    if track is not None:
        fc += f";[vbase][1:v]overlay=0:0:eof_action=pass[vout]"
    else:
        fc += f";[vbase]null[vout]"
    cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", "0:a?",
            "-c:v", "libx264", "-pix_fmt", "yuv420p"] + enc_args + [
            "-c:a", "aac", "-b:a", q["abitrate"], "-t", f"{dur:.3f}",
            "-movflags", "+faststart", str(out_path)]
    ok, err = _run(cmd, timeout=1800)
    if (not ok or not out_path.exists()) and track is not None:
        # Аварийный путь: если лента чем-то не понравилась ffmpeg — рендерим без
        # субтитров, клип важнее.
        print("[clip] caption track failed, rendering without subs:", err[:200])
        return _render_one_clip(ffmpeg, src, start, dur, [], framing, workdir,
                                out_path, q, hook_title="")
    if not ok or not out_path.exists():
        print("[clip] render failed:", err)
        return False
    return True


# ----------------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------------
def render_clips(
    source_path: str | Path,
    moments: List[Dict[str, Any]],
    transcript: Optional[Dict[str, Any]],
    job_dir: str | Path,
    settings: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Render N clips from source_path into job_dir. Raises RuntimeError only if
    ffmpeg is missing or the source is unreadable; individual clip failures are
    skipped so the job still succeeds with whatever rendered."""
    settings = settings or {}
    meta = meta or {}
    transcript = transcript or {"segments": [], "words": []}
    source_path = Path(source_path)
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found (needed to cut clips).")
    if not source_path.exists():
        raise RuntimeError(f"source video not found: {source_path}")

    face_on = settings.get("clip_face_tracking", True)
    caps_on = settings.get("clip_burn_captions", True)
    q = _quality(settings)
    repo_root = Path(__file__).resolve().parents[2]
    output_dir = repo_root / "output"
    latest_dir = output_dir / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)

    hashtags = meta.get("hashtags") or []

    def _do_moment(i: int, m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            start = float(m.get("start", 0.0))
            end = float(m.get("end", 0.0))
        except Exception:
            return None
        if end - start < 1.0:
            return None
        dur = round(end - start, 3)

        workdir = Path(tempfile.mkdtemp(prefix=f"trezzy_clip{i}_"))
        try:
            # Geometry: sample frames → size + face center.
            samples = _sample_frames(ffmpeg, source_path, start, end, workdir)
            size = _frame_size(samples[0]) if samples else None
            if not size:
                print(f"[clip] could not sample frames for moment {i}; skipping")
                return None
            src_w, src_h = size
            face_nx = face_center_x(samples) if face_on else None
            framing = {
                "mode": (settings.get("clip_framing") or "fill").lower(),
                "fill": _crop_geometry(src_w, src_h, face_nx),
                "wide": _crop_geometry_wide(src_w, src_h, face_nx),
            }

            caps = []
            if caps_on:
                # Karaoke word-by-word captions; fall back to phrase-level segments.
                caps = _word_chunks(transcript, start, end) or _clip_caption_lines(transcript, start, end)

            title = (m.get("title") or "").strip()
            out_path = job_dir / f"clip_{i:02d}.mp4"
            t_clip = time.time()
            print(f"[clip] клип {i}/{len(moments)}: {dur:.0f}с, "
                  f"субтитры: {len(caps)} чанк(ов) — рендерю...", flush=True)
            # Хук-заголовок сверху отключён по решению владельца — лишний текст
            # в кадре не нужен, остаются только караоке-субтитры.
            if not _render_one_clip(ffmpeg, source_path, start, dur, caps, framing, workdir,
                                    out_path, q, hook_title=""):
                return None
            if not _verify(ffmpeg, out_path):
                print(f"[clip] clip {i} failed decode verification; skipping")
                return None
            print(f"[clip] клип {i}/{len(moments)} готов за {time.time() - t_clip:.0f}с", flush=True)

            # Per-clip sidecars.
            caption = (m.get("caption") or title or meta.get("topic") or "TREZZY").strip()
            (job_dir / f"clip_{i:02d}.caption.txt").write_text(caption, encoding="utf-8")
            if hashtags:
                (job_dir / f"clip_{i:02d}.hashtags.txt").write_text("\n".join(hashtags) + "\n", encoding="utf-8")
            if caps:
                _write_srt(caps, job_dir / f"clip_{i:02d}.srt")

            return {
                "index": i,
                "path": str(out_path),
                "start": round(start, 2),
                "end": round(end, 2),
                "duration": dur,
                "title": title,
                "caption": caption,
                "reason": (m.get("reason") or "").strip(),
                "score": m.get("score"),
                "hook": (m.get("hook") or "")[:160],
                "face_tracked": face_nx is not None,
                "captions": bool(caps),
            }
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # Два клипа параллельно: один ffmpeg не загружает все ядра, на 4-ядернике
    # это даёт ~1.6-1.8x к скорости всей пачки (жалоба «слишком долго режется»).
    import concurrent.futures as _fut
    with _fut.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_do_moment, i, m) for i, m in enumerate(moments, start=1)]
        # Одна упавшая нарезка НЕ должна ронять весь батч: f.result() ре-кидает
        # исключение воркера. Ловим поштучно → битый момент становится None и
        # отфильтруется ниже (ровно задокументированное «failed clips are skipped»).
        results = []
        for f in futures:
            try:
                results.append(f.result())
            except Exception as e:
                print(f"[clip] момент упал с исключением, пропускаю: {str(e)[:120]}", flush=True)
                results.append(None)
    rendered: List[Dict[str, Any]] = sorted(
        (r for r in results if r), key=lambda r: r["index"])

    if not rendered:
        raise RuntimeError("no clips were produced (all moments failed to render).")

    # final.mp4 = best/first clip → existing preview + mirrors keep working.
    final_mp4 = job_dir / "final.mp4"
    shutil.copyfile(rendered[0]["path"], final_mp4)
    for dst in (latest_dir / "final.mp4", output_dir / "final.mp4"):
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(final_mp4, dst)
        except Exception:
            pass

    (job_dir / "clips.json").write_text(
        json.dumps({"source": str(source_path), "count": len(rendered), "clips": rendered},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "output_path": str(final_mp4),
        "package_dir": str(job_dir),
        "duration_seconds": rendered[0]["duration"],
        "renderer": "clip_engine",
        "clip_count": len(rendered),
        "clips": rendered,
        "transcriber": (transcript or {}).get("provider"),
        "captions": any(c["captions"] for c in rendered),
    }


__all__ = ["render_clips", "video_duration"]
