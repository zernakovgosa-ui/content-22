# -*- coding: utf-8 -*-
"""Визуальный детектор названий казино/контор на кадрах клипа (для блюра).

Best-effort и полностью необязательный: если OCR-движок не установлен или что-то
ломается — возвращаем пустой список, и рендер идёт как обычно (клип важнее).

Идея: семплим кадры клипа → OCR (RapidOCR, ONNX, на CPU, видит лат.+кириллицу) →
текст сверяем со списком брендов (brand_filter) → координаты найденного названия
сливаем в устойчивые регионы {t0,t1,x,y,w,h} в координатах ИСХОДНИКА. Рендер потом
размывает эти прямоугольники ДО кропа/субтитров, поэтому субтитрам не мешает.
"""

from __future__ import annotations

import glob
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from . import brand_filter
except Exception:                       # запуск как одиночный модуль
    import brand_filter  # type: ignore

_OCR = None
_OCR_TRIED = False
# RapidOCR-инстанс ОДИН на процесс, а рендер идёт в 4 потока → конкурентные вызовы
# ocr() могут дедлокнуть/висеть. Сериализуем доступ к движку этим локом.
_OCR_LOCK = threading.Lock()


def _get_ocr():
    """Ленивая единичная загрузка RapidOCR (модель грузится ~1-2с — один раз)."""
    global _OCR, _OCR_TRIED
    if _OCR_TRIED:
        return _OCR
    _OCR_TRIED = True
    try:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
        print("[casino] OCR-движок загружен (RapidOCR)", flush=True)
    except Exception as e:
        print(f"[casino] OCR недоступен ({str(e)[:90]}) — визуальный блюр пропущен "
              f"(pip install rapidocr-onnxruntime)", flush=True)
        _OCR = None
    return _OCR


def _bbox(points: List[List[float]]) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _overlaps(a: Dict[str, float], b: Tuple[float, float, float, float], slack: float = 0.0) -> bool:
    ax0, ay0, ax1, ay1 = a["x"], a["y"], a["x"] + a["w"], a["y"] + a["h"]
    bx0, by0, bx1, by1 = b
    return not (bx0 > ax1 + slack or bx1 < ax0 - slack
               or by0 > ay1 + slack or by1 < ay0 - slack)


def detect_brand_regions(
    ffmpeg: str, src: Path, start: float, dur: float,
    src_w: int, src_h: int,
    settings: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, float]]:
    """Регионы {t0,t1,x,y,w,h} (коорд. ИСХОДНИКА, время clip-relative), где найдено
    название конторы. Пустой список = ничего не нашли / OCR недоступен."""
    settings = settings or {}
    if not brand_filter.filter_enabled(settings):
        return []
    ocr = _get_ocr()
    if ocr is None or dur <= 0:
        return []

    brands = brand_filter.load_brands(settings)
    fps = float(settings.get("casino_blur_fps", 1.0) or 1.0)
    budget_s = float(settings.get("casino_blur_budget_s", 45) or 45)   # жёсткий лимит OCR на клип
    min_conf = float(settings.get("casino_blur_min_conf", 0.45) or 0.45)
    max_regions = int(settings.get("casino_blur_max_regions", 12) or 12)
    pad_s = float(settings.get("casino_blur_pad_s", 0.4) or 0.4)
    time_bridge = float(settings.get("casino_blur_bridge_s", 1.2) or 1.2)
    ocr_w = 1000                                  # ширина кадров для OCR (баланс скорость/читаемость)
    scale = max(1.0, src_w / float(ocr_w)) if src_w else 1.0
    max_frames = int(settings.get("casino_blur_max_frames", 160) or 160)

    work = Path(tempfile.mkdtemp(prefix="trezzy_ocr_"))
    detections: List[Tuple[float, Tuple[float, float, float, float]]] = []
    try:
        pat = (f"fps={fps},scale={ocr_w}:-2" if (src_w and src_w > ocr_w)
               else f"fps={fps}")
        cmd = [ffmpeg, "-y", "-ss", f"{max(0.0, start):.3f}", "-i", str(src),
               "-t", f"{dur:.3f}", "-vf", pat, "-frames:v", str(max_frames),
               str(work / "f_%05d.png")]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
        if p.returncode != 0:
            return []
        frames = sorted(glob.glob(str(work / "f_*.png")))
        t_start = time.time()
        for idx, fp in enumerate(frames):
            if time.time() - t_start > budget_s:   # жёсткий лимит OCR на клип — НЕ вешаемся
                print(f"[casino] лимит OCR ({budget_s:.0f}с) на кадре {idx}/{len(frames)} — стоп",
                      flush=True)
                break
            t = idx / fps                          # время кадра, clip-relative
            try:
                with _OCR_LOCK:                    # один движок на процесс → сериализуем потоки
                    res, _ = ocr(fp)
            except Exception:
                continue
            for item in (res or []):
                try:
                    pts, text, conf = item[0], str(item[1]), float(item[2])
                except Exception:
                    continue
                if conf < min_conf:
                    continue
                if not brand_filter.match_brand(text, brands):
                    continue
                x0, y0, x1, y1 = _bbox(pts)
                detections.append((t, (x0 * scale, y0 * scale, x1 * scale, y1 * scale)))
    except Exception as e:
        print(f"[casino] детектор сбоил ({str(e)[:80]}) — блюр пропущен", flush=True)
        return []
    finally:
        try:
            import shutil
            shutil.rmtree(work, ignore_errors=True)
        except Exception:
            pass

    if not detections:
        return []

    # Слияние во времени+пространстве: логотип обычно стоит на месте → объединяем
    # близкие по месту детекции в один регион, мостим короткие пропуски кадров.
    detections.sort(key=lambda d: d[0])
    regions: List[Dict[str, float]] = []
    for t, box in detections:
        bx0, by0, bx1, by1 = box
        placed = False
        for r in regions:
            if r["last_t"] >= t - time_bridge and _overlaps(r, box, slack=min(src_w, src_h) * 0.04):
                r["x"] = min(r["x"], bx0)
                r["y"] = min(r["y"], by0)
                r["w"] = max(r["x"] + r["w"], bx1) - r["x"]
                r["h"] = max(r["y"] + r["h"], by1) - r["y"]
                r["last_t"] = max(r["last_t"], t)
                placed = True
                break
        if not placed:
            regions.append({"x": bx0, "y": by0, "w": bx1 - bx0, "h": by1 - by0,
                            "first_t": t, "last_t": t})

    out: List[Dict[str, float]] = []
    mx = float(src_w or 10**9)
    my = float(src_h or 10**9)
    for r in regions:
        # поля вокруг названия + кламп в кадр + чётные размеры (нужно ffmpeg crop)
        mgx = max(8.0, r["w"] * 0.10)
        mgy = max(8.0, r["h"] * 0.20)
        x = max(0.0, r["x"] - mgx)
        y = max(0.0, r["y"] - mgy)
        w = min(mx - x, r["w"] + 2 * mgx)
        h = min(my - y, r["h"] + 2 * mgy)
        x, y, w, h = int(x // 2 * 2), int(y // 2 * 2), int(w // 2 * 2), int(h // 2 * 2)
        if w < 12 or h < 8:
            continue
        out.append({"t0": round(max(0.0, r["first_t"] - pad_s), 2),
                    "t1": round(min(dur, r["last_t"] + pad_s), 2),
                    "x": x, "y": y, "w": w, "h": h})
    # самые «долгие» регионы первыми, обрезаем по лимиту
    out.sort(key=lambda r: (r["t1"] - r["t0"]), reverse=True)
    if len(out) > max_regions:
        out = out[:max_regions]
    print(f"[casino] найдено регионов для блюра: {len(out)}", flush=True)
    return out


__all__ = ["detect_brand_regions"]
