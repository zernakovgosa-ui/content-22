# -*- coding: utf-8 -*-
"""Quick clip test: feed a video, get vertical shorts with face-focus + subtitles.

Usage (via TEST_CLIP.bat):
    drag a video onto TEST_CLIP.bat   → clips appear in output/jobs/cliptest_*/
    or put a file in assets/source/ and just run TEST_CLIP.bat

Runs the real pipeline directly (no server): transcribe → ClipAgent → render_clips.
Set env TREZZY_NO_OPEN=1 to skip opening the output folder (used by automated tests).
"""

import os
import sys
import json
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mp4v"}


def _pick_source(argv) -> Path | None:
    if len(argv) > 1 and argv[1].strip():
        return Path(argv[1].strip().strip('"'))
    srcdir = REPO / "assets" / "source"
    vids = sorted(p for p in srcdir.glob("*") if p.suffix.lower() in VIDEO_EXTS)
    return vids[0] if vids else None


def main() -> int:
    from packages.video.transcribe import transcribe
    from packages.video.clip_renderer import render_clips, video_duration
    from packages.agents.clip_agent import ClipAgent
    from packages.agents.base import AgentContext

    src = _pick_source(sys.argv)
    if not src:
        print("[X] Не нашёл видео. Перетащи файл на TEST_CLIP.bat,")
        print("    или положи видео в папку assets\\source\\ и запусти снова.")
        return 1
    if not src.exists():
        print("[X] Файл не найден:", src)
        return 1

    clip_count = 0   # 0 = авто: 2 клипа на каждые 10 минут видео
    if len(sys.argv) > 2:
        try:
            clip_count = max(0, min(30, int(sys.argv[2])))
        except Exception:
            pass

    settings = {}
    try:
        settings = json.loads((REPO / "data" / "settings.json").read_text(encoding="utf-8"))
    except Exception:
        pass

    groq = settings.get("groq_api_key") or ""
    openai = settings.get("openai_api_key") or ""
    anthropic = settings.get("anthropic_api_key") or ""

    print("=" * 60)
    print("  TREZZY — ТЕСТ НАРЕЗКИ")
    print("=" * 60)
    print("  Видео   :", src.name)
    print("  Клипов  :", clip_count if clip_count else "авто (2 на каждые 10 мин)")
    if groq:
        print("  Речь    : Groq Whisper (есть ключ) → субтитры будут")
    elif openai:
        print("  Речь    : OpenAI Whisper (есть ключ) → субтитры будут")
    else:
        print("  Речь    : НЕТ ключа → без субтитров, нарежет равномерно")
    if anthropic or openai or groq:
        prov_name = "Anthropic" if anthropic else ("OpenAI" if openai else "Groq (бесплатный)")
        print(f"  Моменты : умный выбор + оценка виральности ({prov_name} LLM)")
    else:
        print("  Моменты : равномерно (для умного выбора нужен ключ Groq/OpenAI/Anthropic)")
    print("=" * 60)

    t0 = time.time()

    print("\n[1/3] Распознаю речь (может занять время на длинном видео)...")
    tr = transcribe(src, settings)
    print(f"      провайдер={tr.get('provider')}  сегментов={len(tr.get('segments', []))}  ok={tr.get('ok')}")
    if not tr.get("ok") and tr.get("error"):
        print("      причина:", str(tr.get("error"))[:120])

    dur = video_duration(src)
    if not clip_count:
        clip_count = max(2, int(round((dur or 600) / 300.0)))
        print(f"\n      авто-количество: {clip_count} клип(ов) на ~{int((dur or 0) / 60)} мин")
    print(f"\n[2/3] Выбираю моменты (длительность видео ~{int(dur) if dur else '?'}с)...")
    prov = "anthropic" if anthropic else ("openai" if openai else ("groq" if groq else None))
    key = anthropic or openai or groq or None
    ctx = AgentContext(topic=src.stem, product_name=settings.get("default_brand") or "TREZZY", format="clip")
    sel = ClipAgent(llm_provider=prov, llm_key=key).run(
        ctx, transcript=tr, source_duration=dur, target_count=clip_count
    )
    moments = sel.get("moments", [])
    smart = any((m.get("reason") or "").strip() and "fallback" not in (m.get("reason") or "") for m in moments)
    print("      выбор:", "умный + баллы виральности (LLM)" if smart else "равномерный")
    for i, m in enumerate(moments, 1):
        ttl = (m.get("title") or "").strip()[:44]
        sc = m.get("score")
        sc_s = f"[{sc:>3} баллов] " if isinstance(sc, (int, float)) else ""
        print(f"      • клип {i} {sc_s}{m['start']:.0f}–{m['end']:.0f}с  {ttl}")
        hk = (m.get("hook") or "").strip()
        if hk:
            print(f"        хук: «{hk[:70]}»")

    print("\n[3/3] Режу, кадрирую 9:16 с фокусом на лицо, вшиваю субтитры...")
    jobdir = REPO / "output" / "jobs" / ("cliptest_" + time.strftime("%Y%m%d-%H%M%S"))
    try:
        res = render_clips(src, moments, tr, jobdir, settings,
                           meta={"topic": src.stem, "hashtags": []})
    except Exception as e:
        print("[X] Ошибка рендера:", e)
        return 1

    print("\n" + "=" * 60)
    print(f"  ГОТОВО: {res.get('clip_count')} клип(ов) за {round(time.time() - t0, 1)} сек")
    print("  Папка :", jobdir)
    for c in res.get("clips", []):
        sc = c.get("score")
        sc_s = f"балл={sc}, " if isinstance(sc, (int, float)) else ""
        print(f"    - {Path(c['path']).name}  ({sc_s}{c['duration']}с, лицо={'да' if c.get('face_tracked') else 'центр'}, субтитры={'да' if c.get('captions') else 'нет'})")
    print("  (клипы отсортированы по баллу: clip_01 = самый залётный, он же final.mp4)")

    # Telegram review: each clip goes to the owner with ✅/❌ buttons.
    try:
        from packages.integrations.telegram_bot import notify_clips_for_review, review_enabled
        if review_enabled(settings):
            print("\n[TG] Отправляю клипы на одобрение в Telegram...")
            sent = notify_clips_for_review(settings, res.get("clips", []), jobdir.name, src.stem)
            print(f"[TG] Отправлено: {sent}. Нажми ✅/❌ в чате — решение обработается, пока запущен сервер (START.bat).")
    except Exception as e:
        print("[TG] отправка не удалась:", str(e)[:120])
    print("=" * 60)

    if not os.getenv("TREZZY_NO_OPEN"):
        try:
            os.startfile(str(jobdir))  # type: ignore[attr-defined]
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
