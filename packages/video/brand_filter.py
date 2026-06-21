# -*- coding: utf-8 -*-
"""Фильтр брендов казино/букмекеров/скин-сайтов.

Две задачи на одном списке брендов:
  • TEXT-цензура — чтобы НАШИ субтитры/название/описание никогда не называли
    контору (даже если стример произнёс «1win» и Whisper это распознал).
  • детектор — даёт список токенов для визуального OCR-блюра (casino_blur.py).

Список курируемый и РАСШИРЯЕМЫЙ через settings["casino_brands"] (без правки кода).
Берём ТОЛЬКО различимые токены — короткие общеупотребимые слова не включаем,
чтобы не зацензурить обычную речь. Многословные бренды требуют слова-якоря
(«… casino»), что тоже отсекает ложные срабатывания.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Различимые бренды (латиница + кириллица). Двусмысленные короткие слова
# (sol/jet/lex/cat/drip/leon…) даём ТОЛЬКО в форме «… casino/каzино», иначе бы
# мазали обычную речь. Пользователь дополняет список в settings.json.
DEFAULT_BRANDS: List[str] = [
    # — казино —
    "1win", "1вин", "ван вин", "vavada", "вавада", "mostbet", "мостбет",
    "1xbet", "1хбет", "1xslots", "1xstavka", "1хставка", "melbet", "мелбет",
    "pin-up", "pinup", "пинап", "пин ап", "azino777", "azino 777", "азино",
    "joycasino", "джойказино", "pokerdom", "покердом", "888starz", "dragon money",
    "play fortuna", "плей фортуна", "vulkan vegas", "вулкан вегас", "vulkan royal",
    "riobet", "риобет", "up-x", "ап икс", "7k casino", "legzo", "легзо",
    "gizbo", "гизбо", "sykaaa", "daddy casino", "дэдди", "drip casino", "gama casino",
    "monro casino", "sol casino", "jet casino", "lex casino", "booi casino",
    "champion casino", "selector casino", "kometa casino", "starda casino",
    "irwin casino", "vodka casino", "banda casino", "izzi casino", "cat casino",
    "r7 casino", "fresh casino", "rox casino", "slottica", "слоттика",
    # — букмекеры —
    "winline", "винлайн", "fonbet", "фонбет", "betboom", "бетбум",
    "liga stavok", "лига ставок", "leonbets", "леонбетс", "olimpbet", "олимпбет",
    "marathonbet", "марафонбет", "parimatch", "париматч", "betcity", "бетсити",
    "baltbet", "балтбет", "tennisi", "тенниси", "astrabet", "pari ru", "пари ру",
    # — CS:GO / скины —
    "csgorun", "csgo run", "csgoroll", "csgofast", "csgoempire", "csgo empire",
    "hellcase", "хеллкейс", "key-drop", "keydrop", "кейдроп", "datdrop", "gamdom",
    "farmskins", "ggdrop", "gg drop", "roobet", "clash.gg", "skinclub", "skin club",
    "tradeit", "bandit.camp", "stake.com", "csgo500", "500 casino", "rustchance",
]

_MASK = "***"
_CACHE: Dict[int, "re.Pattern[str]"] = {}


def load_brands(settings: Optional[Dict[str, Any]]) -> List[str]:
    """Список брендов из настроек (casino_brands) или дефолтный. Пустой/битый → дефолт."""
    if settings:
        raw = settings.get("casino_brands")
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace("\n", ",").split(",")]
        if isinstance(raw, list):
            got = [str(x).strip() for x in raw if str(x).strip()]
            if got:
                return got
    return DEFAULT_BRANDS


def filter_enabled(settings: Optional[Dict[str, Any]]) -> bool:
    return bool((settings or {}).get("casino_filter_enabled", True))


def _pattern(brands: List[str]) -> "re.Pattern[str]":
    key = hash(tuple(brands))
    pat = _CACHE.get(key)
    if pat is not None:
        return pat
    parts = []
    for b in brands:
        b = b.strip().lower()
        if not b:
            continue
        # внутренние пробелы/дефисы/точки → гибкий разделитель (1 win, csgo-run, pin.up)
        esc = re.escape(b)
        esc = re.sub(r"\\[\s\-_.]", r"[\\s\\-_.]*", esc)
        parts.append(esc)
    if not parts:
        parts = ["(?!x)x"]   # ничего не матчит
    # границы по букве/цифре (работает и для кириллицы, и для «1win»)
    body = "|".join(sorted(parts, key=len, reverse=True))
    pat = re.compile(rf"(?<![0-9a-zA-Zа-яёА-ЯЁ])(?:{body})(?![0-9a-zA-Zа-яёА-ЯЁ])",
                     re.IGNORECASE | re.UNICODE)
    _CACHE[key] = pat
    return pat


def contains_brand(text: str, brands: Optional[List[str]] = None) -> bool:
    if not text:
        return False
    return bool(_pattern(brands or DEFAULT_BRANDS).search(text))


def censor_text(text: str, brands: Optional[List[str]] = None, mask: str = _MASK) -> str:
    """Заменить упоминания контор на маску (по умолчанию ***). Сохраняет остальной
    текст и пробелы — для субтитров/названий/описаний."""
    if not text:
        return text
    out = _pattern(brands or DEFAULT_BRANDS).sub(mask, text)
    return re.sub(r"\s{2,}", " ", out).strip()


def match_brand(text: str, brands: Optional[List[str]] = None) -> Optional[str]:
    """Нормализованный текст с OCR → найденный бренд (или None). Терпимо к регистру
    и мусорным символам вокруг; используется визуальным детектором."""
    if not text:
        return None
    m = _pattern(brands or DEFAULT_BRANDS).search(text)
    return m.group(0) if m else None


__all__ = ["DEFAULT_BRANDS", "load_brands", "filter_enabled",
           "contains_brand", "censor_text", "match_brand"]
