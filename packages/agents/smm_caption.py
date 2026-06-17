# -*- coding: utf-8 -*-
"""SMMCaptionAgent — produces caption, hashtags, short title, platform notes."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import AgentContext, BaseAgent

_WORKER_DIR = Path(__file__).resolve().parents[2] / "trezzy-video-worker"


def _import_brain():
    if str(_WORKER_DIR) not in sys.path:
        sys.path.insert(0, str(_WORKER_DIR))
    import content_brain  # type: ignore
    return content_brain


_PLATFORM_NOTES = {
    "instagram":
        "Caption средней длины. 5–9 хэштегов. Первый хэштег — бренд. "
        "Эмодзи можно один, в начале. CTA в последней строке.",
    "tiktok":
        "Caption очень короткий, цепляет в первой строке. Хэштеги отдельной строкой. "
        "Тренды трекать вручную — добавь 1–2 трендовых тега.",
    "youtube":
        "Заголовок ≤ 60 символов. Description начинается с ключевого запроса. "
        "Хэштеги в конце description (#trezzy #parfum + 3 целевых).",
    "all":
        "Используй универсальный caption: короткая первая строка-хук, "
        "1 строка контекста, CTA, хэштеги отдельным блоком.",
}


def _short_title(hook: str, max_len: int = 55) -> str:
    h = hook.strip().strip("«»\".'")
    if len(h) <= max_len:
        return h
    return h[: max_len - 1].rstrip(",.;: ") + "…"


class SMMCaptionAgent(BaseAgent):
    name = "smm_caption"

    def _local(
        self,
        ctx: AgentContext,
        hook: Optional[str] = None,
        script: Optional[str] = None,
        cta: Optional[str] = None,
        vibe_tags: Optional[List[str]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        brain = _import_brain()
        # If the caller didn't pass a hook (single-agent usage), build a plan to
        # bootstrap one. In the orchestrated pipeline the ScriptWriter has
        # already produced these, so we just rewrap them.
        if not hook or not script or not cta:
            plan = brain.make_plan(
                topic=ctx.topic,
                product_name=ctx.product_name,
                target_audience=ctx.target_audience,
                style=ctx.style,
                format=ctx.format,
                seed=ctx.seed,
            )
            hook = hook or plan["hook"]
            script = script or plan["script"]
            cta = cta or plan["cta"]
            vibe_tags = vibe_tags or plan["vibe_tags"]

        # Hashtags piggy-back on the content_brain logic for consistency.
        hashtags = brain._gen_hashtags(ctx.format, ctx.target_audience, ctx.product_name)
        first_sentence = brain._first_sentence(script)
        caption = f"{hook}\n\n{first_sentence}\n\n{cta}."

        platform_key = ctx.platform if ctx.platform in _PLATFORM_NOTES else "all"

        return {
            "caption":        caption,
            "hashtags":       hashtags,
            "short_title":    _short_title(hook),
            "platform_notes": _PLATFORM_NOTES[platform_key],
        }
