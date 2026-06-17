# -*- coding: utf-8 -*-
"""VisualDirectorAgent — translates the script into visual instructions."""

from __future__ import annotations

import random
from typing import Any, Dict, List

from .base import AgentContext, BaseAgent

_BG_MOODS = {
    "single_review":    "очень тёмный графитовый фон, тёплое золотое пятно света справа от центра",
    "top_list":         "три коротких блока на одном фоне, тёплые точечные пятна света для каждого",
    "mood_story":       "вечер, низкий свет, тёплый шлейф через кадр",
    "celebrity_style":  "moody low-key, минимум деталей, один источник света",
    "problem_solution": "первая половина — холодная и плоская, вторая — тёплая и объёмная",
    "luxury_quote":     "абсолютная чернота, одна цитата большими буквами, без объектов",
    "date_night":       "свечи, шёлк, тёмный бар, тёплая бронза",
    "office_rich":      "панорама города в большом окне, костюм, спокойный дневной свет",
    "quiet_luxury":     "монохром бежевый/серый, лён, бумага, без логотипов",
    "perfume_for_mood": "сцена-настроение под topic, минимум деталей, один продуктовый кадр",
    "ai_ugc_ad":        "AI-аватар или face cam в центре, тёплый фон с золотыми частицами, fast cuts",
}

_SHOT_LIBRARY = [
    "extreme close-up на текстуре стекла флакона",
    "медленный pan по силуэту человека на тёмном фоне",
    "капля жидкости, замедленная съёмка",
    "пар / лёгкий дым у горловины",
    "рука берёт флакон со столика, фокус на запястье",
    "блик света по гранёному стеклу",
    "тёплый шлейф у воротника рубашки",
    "силуэт против окна, контражур",
]

_TEXT_LAYOUTS = [
    "крупная цитата по центру, гарнитура с засечками",
    "одна строка снизу, золотая подчёркивающая линия",
    "три строки caps lock, узкое межстрочье",
    "минимум текста, только название бренда + одна нота",
]

_CAPCUT_EDIT_NOTES = [
    "Импортируй final.mp4. Поверх — авто-субтитры белым sans-serif с золотой подчеркиванием.",
    "Добавь light-leak overlay только на HOOK и CTA. Не на MAIN.",
    "Музыка: cinematic / niche perfume ambient. -18 LUFS. Под voiceover -6 dB.",
    "Зерно плёнки 5–10%. Виньетка лёгкая. Без spin / glitch.",
    "Экспорт 1080×1920 H.264 8–10 Мбит/с, AAC 192 kbps.",
]

_ASSET_SUGGESTIONS = [
    "тёмная подложка с тёплым градиентом (assets/backgrounds/)",
    "макро-кадр флакона (assets/perfume/)",
    "light-leak оверлей (assets/overlays/)",
    "ambient-трек 90–120 BPM (assets/music/)",
    "пара золотых тонкостей: линия, ромб, точка",
]


class VisualDirectorAgent(BaseAgent):
    name = "visual_director"

    def _local(self, ctx: AgentContext, **extra: Any) -> Dict[str, Any]:
        rng = random.Random(ctx.seed)
        bg = _BG_MOODS.get(ctx.format, _BG_MOODS["single_review"])
        shots = rng.sample(_SHOT_LIBRARY, k=3)
        layout = rng.choice(_TEXT_LAYOUTS)
        capcut_notes: List[str] = list(_CAPCUT_EDIT_NOTES)  # all five, deterministic
        assets = rng.sample(_ASSET_SUGGESTIONS, k=3)

        return {
            "background_mood":         bg,
            "shot_ideas":              shots,
            "text_layout_notes":       layout,
            "capcut_edit_notes":       capcut_notes,
            "visual_asset_suggestions": assets,
        }
