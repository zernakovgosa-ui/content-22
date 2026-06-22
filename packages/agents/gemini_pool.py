# -*- coding: utf-8 -*-
"""Пул Gemini-ключей с авто-ротацией при лимите (429/503).

Бесплатный Gemini имеет минутные/суточные лимиты НА ПРОЕКТ. Умный отбор моментов
на длинном видео шлёт много запросов подряд (по куску транскрипта) — один ключ
быстро упирается в 429, и весь отбор сваливается в тупой fill (клипы по 32с,
балл 50). Владелец даёт НЕСКОЛЬКО ключей (лучше из РАЗНЫХ Google-проектов/аккаунтов —
квота на проект): при 429 переключаемся на следующий и повторяем.

Источник правды — data/settings.json: список `gemini_api_keys` + одиночный
`gemini_api_key` как запасной. Читается лениво, перечитывается при смене mtime —
ключ можно добавить без перезапуска. Зеркало groq_pool.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import List

_LOCK = threading.RLock()
_SETTINGS = Path(__file__).resolve().parents[2] / "data" / "settings.json"
_state = {"keys": [], "idx": 0, "mtime": -1.0}


def _load() -> None:
    try:
        mt = _SETTINGS.stat().st_mtime
    except Exception:
        return
    with _LOCK:
        if mt == _state["mtime"] and _state["keys"]:
            return
    try:
        s = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return
    raw = s.get("gemini_api_keys") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    single = s.get("gemini_api_key") or ""
    keys: List[str] = []
    for k in list(raw) + [single]:
        k = (k or "").strip()
        if k and k not in keys:
            keys.append(k)
    with _LOCK:
        _state["keys"] = keys
        _state["mtime"] = mt
        if _state["idx"] >= len(keys):
            _state["idx"] = 0


def keys() -> List[str]:
    _load()
    with _LOCK:
        return list(_state["keys"])


def count() -> int:
    return len(keys())


def current() -> str:
    _load()
    with _LOCK:
        return _state["keys"][_state["idx"]] if _state["keys"] else ""


def rotate() -> str:
    """Переключиться на следующий ключ (при 429/503). Вернуть новый текущий."""
    with _LOCK:
        if len(_state["keys"]) > 1:
            _state["idx"] = (_state["idx"] + 1) % len(_state["keys"])
        return _state["keys"][_state["idx"]] if _state["keys"] else ""


__all__ = ["keys", "count", "current", "rotate"]
