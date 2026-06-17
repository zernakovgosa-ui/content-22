# -*- coding: utf-8 -*-
"""MarketingStrategistAgent — picks the angle for a TREZZY short.

Inputs : topic, product_name, target_audience, platform, format
Output : angle, emotion, promise, objection, cta_strategy
"""

from __future__ import annotations

import random
from typing import Any, Dict

from .base import AgentContext, BaseAgent

_ANGLE_BY_FORMAT = {
    "single_review":    "продуктовый обзор через ощущение, не через ноты",
    "top_list":         "капсула из трёх ароматов под разные сцены",
    "mood_story":       "атмосферная сцена, аромат как герой кадра",
    "celebrity_style":  "архетип / икона стиля, без имитации звезды",
    "problem_solution": "первое впечатление — это аромат, а не одежда",
    "luxury_quote":     "одна цитата + чёрный экран + большая типографика",
    "date_night":       "вечер, тёплый шлейф, причина подойти ближе",
    "office_rich":      "переговорка, костюм, спокойная уверенность",
    "quiet_luxury":     "без логотипа, без громких аккордов, только присутствие",
    "perfume_for_mood": "аромат как кнопка переключения состояния",
    "ai_ugc_ad":        "разговорный POV / честный отзыв в стиле TikTok UGC",
}

_EMOTION_BY_FORMAT = {
    "single_review":    "уверенное спокойствие",
    "top_list":         "контроль и выбор",
    "mood_story":       "вечерняя интимность",
    "celebrity_style":  "статус без объяснений",
    "problem_solution": "лёгкое узнавание себя",
    "luxury_quote":     "тишина и пауза",
    "date_night":       "тёплое притяжение",
    "office_rich":      "собранная уверенность",
    "quiet_luxury":     "достоинство без шума",
    "perfume_for_mood": "управляемое настроение",
    "ai_ugc_ad":        "узнавание и доверие",
}

_PROMISE_BY_PLATFORM = {
    "instagram":      "тебя начнут считывать дороже",
    "tiktok":         "за 10 секунд ты поймёшь, чего тебе не хватало",
    "youtube":        "разберёмся, почему этот аромат запоминают",
    "all":            "новая подпись, которую слышат в комнате",
}

_OBJECTIONS = [
    "слишком дорого для повседнева",
    "пахнет «как у всех»",
    "не подходит под мой образ",
    "не уверен, что это «моё»",
    "уже есть любимый парфюм",
]

_CTA_STRATEGIES = [
    "soft CTA в конце: пригласи на TREZZY, без давления",
    "хук + scarcity: «лимитированный завоз — найди свой»",
    "сначала бесплатный подбор по настроению, потом ссылка",
    "сравни с тем, что уже есть в коллекции — потом TREZZY",
    "сначала ощущение, потом продукт, в конце адрес",
]


class MarketingStrategistAgent(BaseAgent):
    name = "marketing_strategist"

    def _local(self, ctx: AgentContext, **extra: Any) -> Dict[str, Any]:
        rng = random.Random(ctx.seed)
        platform_key = ctx.platform if ctx.platform in _PROMISE_BY_PLATFORM else "all"

        return {
            "angle":        _ANGLE_BY_FORMAT.get(ctx.format, _ANGLE_BY_FORMAT["single_review"]),
            "emotion":      _EMOTION_BY_FORMAT.get(ctx.format, "уверенность"),
            "promise":      _PROMISE_BY_PLATFORM[platform_key],
            "objection":    rng.choice(_OBJECTIONS),
            "cta_strategy": rng.choice(_CTA_STRATEGIES),
            "audience":     ctx.target_audience or "взрослая аудитория, ценит вкус",
        }
