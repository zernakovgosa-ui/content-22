# -*- coding: utf-8 -*-
"""ScriptWriterAgent — turns the strategy into hook + script + scenes + VO."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List

from .base import AgentContext, BaseAgent

# Reuse the worker's content_brain so hooks/scripts/vibes/edit_notes stay in
# one place. Importing across the repo without packaging — add the worker to
# sys.path lazily on first use.
_WORKER_DIR = Path(__file__).resolve().parents[2] / "trezzy-video-worker"


def _import_brain():
    if str(_WORKER_DIR) not in sys.path:
        sys.path.insert(0, str(_WORKER_DIR))
    import content_brain  # type: ignore
    return content_brain


def _split_script_into_scenes(script: str) -> List[Dict[str, Any]]:
    """Mirror the worker's chunking for the editor-facing scene plan."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", script.strip()) if s.strip()]
    if not sentences:
        sentences = [script.strip()]
    first = sentences[0]
    if first.count(",") >= 2:
        parts = [p.strip() for p in re.split(r"(?<=,)\s+", first) if p.strip()]
        if 2 <= len(parts) <= 4:
            sentences = parts[:3]
    else:
        sentences = sentences[:3]

    return [
        {
            "idx": i + 1,
            "duration_s": 1.6,
            "on_screen": line,
            "voiceover": line,
        }
        for i, line in enumerate(sentences)
    ]


class ScriptWriterAgent(BaseAgent):
    name = "script_writer"

    def _llm(self, ctx: AgentContext, **extra: Any) -> Dict[str, Any]:
        """Generate a topic-driven short-form script via the configured LLM.

        Critically: the TOPIC is the subject of the video. If the topic is a
        partnership program, the script is about the partnership — not perfume.
        """
        from . import llm_client

        product = ctx.product_name or "TREZZY"
        audience = ctx.target_audience or "широкая аудитория"
        platform = ctx.platform or "instagram"
        fmt = ctx.format or "single_review"

        system = (
            "Ты — профессиональный сценарист коротких вертикальных рекламных роликов "
            "(TikTok/Reels/Shorts) на русском языке. Пишешь живо, естественно, без "
            "канцелярита и клише. Твоя задача — сценарий строго по ЗАДАННОЙ ТЕМЕ. "
            "Тема — это и есть предмет ролика; не подменяй её другой темой. "
            "Отвечай ТОЛЬКО валидным JSON без markdown и пояснений."
        )
        user = (
            f"Сделай сценарий короткого ролика (5–8 секунд, ~2–3 коротких фразы).\n\n"
            f"ТЕМА (главное, ролик именно про это): {ctx.topic}\n"
            f"Бренд: {product}\n"
            f"Платформа: {platform}\n"
            f"Формат: {fmt}\n"
            f"Аудитория: {audience}\n"
            f"Стиль подачи: {ctx.style}\n\n"
            "Верни JSON с полями:\n"
            "{\n"
            '  "title": "короткий заголовок ролика",\n'
            '  "hook": "цепляющая первая фраза (до ~8 слов)",\n'
            '  "script": "весь текст озвучки, 2-3 коротких предложения, разговорный тон",\n'
            '  "cta": "призыв к действию в конце",\n'
            '  "vibe_tags": ["3", "коротких", "тега настроения"]\n'
            "}\n\n"
            "ВАЖНО: и hook, и script, и cta должны быть строго про заданную ТЕМУ. "
            "Если тема — партнёрская программа, говори про партнёрство и выгоду, а не про сам товар."
        )

        data = llm_client.complete_json(
            self.llm_provider, self.llm_key, system, user, max_tokens=900, temperature=0.85
        )

        # Validate / coerce shape; fall back happens in run() if this raises.
        hook = str(data["hook"]).strip()
        script = str(data["script"]).strip()
        cta = str(data.get("cta", "")).strip()
        title = str(data.get("title", ctx.topic)).strip()
        vibe = data.get("vibe_tags") or []
        if not isinstance(vibe, list):
            vibe = [str(vibe)]
        vibe = [str(v).strip() for v in vibe][:3]
        if not (hook and script):
            raise ValueError("LLM returned empty hook/script")

        scenes = _split_script_into_scenes(script)
        return {
            "hook":            hook,
            "script":          script,
            "scenes":          scenes,
            "voiceover_text":  script,
            "cta":             cta,
            "vibe_tags":       vibe,
            "title":           title,
            "format":          ctx.format,
        }

    def _local(self, ctx: AgentContext, **extra: Any) -> Dict[str, Any]:
        brain = _import_brain()
        plan = brain.make_plan(
            topic=ctx.topic,
            product_name=ctx.product_name,
            target_audience=ctx.target_audience,
            style=ctx.style,
            format=ctx.format,
            seed=ctx.seed,
        )
        scenes = _split_script_into_scenes(plan["script"])
        return {
            "hook":            plan["hook"],
            "script":          plan["script"],
            "scenes":          scenes,
            "voiceover_text":  plan["script"],
            "cta":             plan["cta"],
            "vibe_tags":       plan["vibe_tags"],
            "title":           plan["title"],
            "format":          plan["format"],
        }
