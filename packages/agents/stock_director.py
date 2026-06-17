# -*- coding: utf-8 -*-
"""StockDirectorAgent — turns a topic/script into stock-footage search queries.

For render_mode="real": we assemble REAL Pexels clips, so we need ENGLISH,
visually-concrete search terms that pull authentic, handheld, lifestyle footage
(not staged "stocky" stuff). Subclasses BaseAgent → _llm() with fallback to
_local() so the pipeline never breaks.

Return shape: {"queries": ["perfume bottle close up", "woman getting ready", ...]}
"""

from __future__ import annotations

from typing import Any, Dict, List

from .base import AgentContext, BaseAgent

# Authentic, perfume/lifestyle-friendly defaults (used when no LLM key is set).
_DEFAULT_QUERIES = [
    "perfume bottle close up",
    "woman getting ready morning",
    "luxury lifestyle aesthetic",
    "soft sunlight window interior",
    "elegant woman portrait candid",
    "city evening lights bokeh",
    "silk fabric flowing slow motion",
    "hands holding glass bottle",
    "golden hour skin glow",
    "cozy cafe aesthetic vertical",
]


class StockDirectorAgent(BaseAgent):
    name = "stock_director"

    def _llm(self, ctx: AgentContext, **extra: Any) -> Dict[str, Any]:
        from . import llm_client

        count = int(extra.get("count") or 5)
        hook = (extra.get("hook") or "").strip()
        script = (extra.get("script") or "").strip()
        vibe = extra.get("vibe_tags") or []
        brand = ctx.product_name or "TREZZY"

        system = (
            "You are a short-form video b-roll director. Given a perfume ad's topic "
            "and script, output ENGLISH stock-footage search queries that find REAL, "
            "authentic, handheld, lifestyle vertical clips on Pexels. Prefer candid, "
            "cinematic-but-real, human moments; AVOID generic corporate 'stock' vibes. "
            "Reply ONLY with valid JSON, no markdown."
        )
        user = (
            f"Brand: {brand} (perfume).\n"
            f"Topic: {ctx.topic}\n"
            f"Hook: {hook}\n"
            f"Script: {script}\n"
            f"Vibe: {', '.join(str(v) for v in vibe)}\n\n"
            f"Give {count} search queries, each 2-4 words, visually concrete, in ENGLISH.\n"
            'Return JSON: {"queries": ["...", "..."]}'
        )

        data = llm_client.complete_json(
            self.llm_provider, self.llm_key, system, user, max_tokens=400, temperature=0.7
        )
        raw = data.get("queries") if isinstance(data, dict) else None
        queries = [str(q).strip() for q in (raw or []) if str(q).strip()][:count]
        if not queries:
            raise ValueError("LLM returned no stock queries")
        return {"queries": queries}

    def _local(self, ctx: AgentContext, **extra: Any) -> Dict[str, Any]:
        count = int(extra.get("count") or 5)
        return {"queries": _DEFAULT_QUERIES[:count]}
