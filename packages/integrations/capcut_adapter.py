# -*- coding: utf-8 -*-
"""CapCut adapter — builds a per-job hand-off checklist.

We do NOT try to remote-control CapCut. Instead we generate a Markdown
checklist that the human editor opens next to final.mp4.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class CapCutAdapter:
    name = "capcut"

    def build_checklist(
        self,
        hook: str,
        cta: str,
        format: str,
        platform: str,
        vibe_tags: Optional[List[str]] = None,
        edit_notes: Optional[List[str]] = None,
    ) -> str:
        tags = ", ".join(vibe_tags or [])
        extra_notes = "\n".join(f"- [ ] {n}" for n in (edit_notes or []))
        return (
            "# CapCut hand-off — TREZZY short\n\n"
            f"**Hook:** {hook}\n"
            f"**CTA:** {cta}\n"
            f"**Format:** `{format}`  ·  **Platform:** `{platform}`\n"
            f"**Vibe:** {tags}\n\n"
            "## Импорт\n"
            "- [ ] Открой CapCut Desktop → New Project.\n"
            "- [ ] Canvas: **1080 × 1920** (9:16).\n"
            "- [ ] Перетащи `final.mp4` на таймлайн.\n\n"
            "## Аудио\n"
            "- [ ] Добавь voiceover (RU, спокойный, низкий тембр).\n"
            "- [ ] Music: cinematic / niche perfume ambient. Target -18 LUFS.\n"
            "- [ ] Duck music под voiceover (-6 dB).\n\n"
            "## Субтитры\n"
            "- [ ] Auto-subtitles → язык RU.\n"
            "- [ ] Style: white sans-serif, gold underline, центр, нижняя треть.\n"
            "- [ ] Удали эмодзи и заглавные ALL CAPS, если попали.\n\n"
            "## Переходы и эффекты\n"
            "- [ ] Только fade / dissolve. Не используй spin / glitch / whip.\n"
            "- [ ] Film grain 5–10%.\n"
            "- [ ] Light leak overlay только на HOOK и CTA.\n"
            "- [ ] Виньетка лёгкая.\n\n"
            "## Дополнительно (из visual_director)\n"
            f"{extra_notes if extra_notes else '- [ ] Дополнительных правок нет.'}\n\n"
            "## Экспорт\n"
            "- [ ] 1080×1920 MP4, H.264, 8–10 Mbps.\n"
            "- [ ] AAC аудио 192 kbps.\n"
            "- [ ] Имя файла: `trezzy_{format}_v1.mp4`.\n"
        )

    def publish(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"status": "manual", "note": "CapCut handoff is manual — see capcut_checklist.md"}
