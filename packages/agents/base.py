# -*- coding: utf-8 -*-
"""Base agent class.

Today every agent uses deterministic / template-based logic (`_local()`).
Tomorrow `_llm()` can be wired to OpenAI / Claude without touching callers —
`run()` calls `_llm()` first and falls back to `_local()` if no key is set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class AgentContext:
    """Lightweight context passed between agents during one job."""
    topic: str
    product_name: Optional[str] = None
    target_audience: Optional[str] = None
    platform: str = "instagram"
    format: str = "single_review"
    style: str = "premium luxury perfume"
    seed: Optional[int] = None


class BaseAgent:
    name: str = "base"

    def __init__(self, llm_provider: Optional[str] = None, llm_key: Optional[str] = None):
        # `llm_provider` can be "openai" | "anthropic" | None.
        # When None, we use local templated logic — perfect for the offline MVP.
        self.llm_provider = llm_provider
        self.llm_key = llm_key or self._env_key(llm_provider)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
    def run(self, ctx: AgentContext, **extra: Any) -> Dict[str, Any]:
        if self.llm_provider and self.llm_key:
            try:
                return self._llm(ctx, **extra)
            except Exception as e:
                # LLM failure must never break the pipeline — but make it visible.
                print("[agent] LLM failed, fallback to template:", repr(e))
        return self._local(ctx, **extra)

    # ------------------------------------------------------------------
    # Override in subclasses
    # ------------------------------------------------------------------
    def _local(self, ctx: AgentContext, **extra: Any) -> Dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def _llm(self, ctx: AgentContext, **extra: Any) -> Dict[str, Any]:
        # Placeholder. Wire an SDK call here when an API key is available.
        # Keep the return shape identical to _local() so callers don't care.
        raise NotImplementedError("LLM provider not wired in this build")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _env_key(provider: Optional[str]) -> Optional[str]:
        if provider == "openai":
            return os.getenv("OPENAI_API_KEY") or None
        if provider == "anthropic":
            return os.getenv("ANTHROPIC_API_KEY") or None
        return None
