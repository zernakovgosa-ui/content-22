# -*- coding: utf-8 -*-
"""Пул Groq-ключей с авто-ротацией при лимите (429).

Бесплатный тариф Groq имеет минутные и суточные лимиты — на длинном видео один
ключ быстро упирается (из-за этого час распознавался на 7 минут). Владелец даёт
НЕСКОЛЬКО ключей: при 429 молча переключаемся на следующий и повторяем запрос.

Источник правды — общий data/settings.json (ключ `groq_api_keys` — список, плюс
одиночный `groq_api_key` как запасной). Читается лениво, перечитывается при смене
mtime файла, поэтому добавить ключ можно без перезапуска. Индекс текущего ключа
общий на процесс — все вызовы (распознавание, выбор моментов, мета) делят прогресс
ротации, так что после 429 следующий вызов стартует уже с рабочего ключа.
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
    raw = s.get("groq_api_keys") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    single = s.get("groq_api_key") or ""
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
    """Переключиться на следующий ключ (вызывается при 429). Вернуть новый текущий."""
    with _LOCK:
        if len(_state["keys"]) > 1:
            _state["idx"] = (_state["idx"] + 1) % len(_state["keys"])
        return _state["keys"][_state["idx"]] if _state["keys"] else ""


__all__ = ["keys", "count", "current", "rotate"]
