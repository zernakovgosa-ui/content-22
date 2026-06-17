# -*- coding: utf-8 -*-
"""QualityControlAgent — validates a generated plan against TREZZY brand rules."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import AgentContext, BaseAgent

_BANNED_PHRASES = [
    "discover the magic",
    "amazing",
    "incredible",
    "wow",
    "лучший в мире",
    "самый лучший",
    "магия",
    "волшеб",  # волшебный, волшебство
    "невероятн",
    "купи прямо сейчас",
]

_MAX_HOOK_CHARS = 60
_MAX_SCRIPT_CHARS = 240
_MIN_TAGS = 2


class QualityControlAgent(BaseAgent):
    name = "quality_control"

    def _local(
        self,
        ctx: AgentContext,
        hook: Optional[str] = None,
        script: Optional[str] = None,
        caption: Optional[str] = None,
        hashtags: Optional[List[str]] = None,
        vibe_tags: Optional[List[str]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        checks: List[Dict[str, Any]] = []
        warnings: List[str] = []
        improvements: List[str] = []

        def check(name: str, passed: bool, detail: str = "") -> None:
            checks.append({"name": name, "passed": passed, "detail": detail})

        # Hook
        if hook is None or not hook.strip():
            check("hook_present", False, "Hook is empty")
            warnings.append("Hook is missing")
        else:
            check("hook_present", True)
            if len(hook) > _MAX_HOOK_CHARS:
                warnings.append(f"Hook is long ({len(hook)} chars). Aim for ≤ {_MAX_HOOK_CHARS}.")
                improvements.append("Сократи hook до одной короткой строки.")
            check("hook_length", len(hook) <= _MAX_HOOK_CHARS, f"{len(hook)} chars")

        # Script
        if script is None or not script.strip():
            check("script_present", False, "Script is empty")
            warnings.append("Script is missing")
        else:
            check("script_present", True)
            if len(script) > _MAX_SCRIPT_CHARS:
                warnings.append(f"Script is long ({len(script)} chars). Aim for ≤ {_MAX_SCRIPT_CHARS}.")
                improvements.append("Сократи script — 2–3 коротких предложения, ритмично.")
            check("script_length", len(script) <= _MAX_SCRIPT_CHARS, f"{len(script)} chars")

        # Banned phrases
        joined = " ".join(filter(None, [hook, script, caption])).lower()
        bad = [p for p in _BANNED_PHRASES if p in joined]
        if bad:
            warnings.append(f"Найдены избитые фразы: {', '.join(bad)}")
            improvements.append("Замени восторженные эпитеты на конкретику ощущений.")
        check("brand_voice_no_cringe", not bad, ", ".join(bad) if bad else "")

        # Vibe tags
        n_tags = len(vibe_tags or [])
        check("vibe_tags_count", n_tags >= _MIN_TAGS, f"{n_tags} tags")
        if n_tags < _MIN_TAGS:
            warnings.append(f"Слишком мало vibe-тегов ({n_tags}). Минимум {_MIN_TAGS}.")

        # Hashtags
        n_ht = len(hashtags or [])
        check("hashtags_count", 3 <= n_ht <= 15, f"{n_ht} hashtags")
        if n_ht < 3:
            improvements.append("Добавь 3–5 целевых хэштегов под формат.")
        if n_ht > 15:
            improvements.append("Срежь хэштеги до 10–15 — алгоритмы не любят флуд.")

        ready = all(c["passed"] for c in checks)

        return {
            "checks":       checks,
            "warnings":     warnings,
            "improvements": improvements,
            "ready":        ready,
            "score":        round(100 * sum(1 for c in checks if c["passed"]) / max(1, len(checks)), 1),
        }
