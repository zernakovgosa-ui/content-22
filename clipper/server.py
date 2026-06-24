# -*- coding: utf-8 -*-
"""Нарезчик — standalone clip-factory tool (port 8002).

Separate from the TREZZY factory (which stays untouched): drop long videos into
category folders, batch-cut them into ranked vertical shorts, approve each clip
in Telegram (✅/❌), distribute approved clips evenly across that category's
accounts, get a steady 2-posts-per-day plan, enter view counts, and receive
🚀 (clip went hot) and ⚠️ (account looks shadow-banned) notifications.

Publishing is the owner-in-the-loop kind: at slot time the bot sends the video
file + caption and the owner posts it manually (Правило №3: никакого автологина).

Reuses the factory's engine: transcribe → ClipAgent (virality scoring) →
render_clips (face-aware 9:16, karaoke captions). One JSON state file, one lock.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Консоль Windows бывает cp1251 — принудительно UTF-8, иначе эмодзи/стрелки в логах
# роняют print с UnicodeEncodeError. А если это случится в ветке УСПЕХА (например
# «cobalt ✓»), успешное скачивание ложно засчитается как сбой. errors=replace —
# даже неожиданный символ не уронит процесс.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CLIPPER_DIR = Path(__file__).resolve().parent
REPO = CLIPPER_DIR.parent
sys.path.insert(0, str(REPO))

from fastapi import FastAPI, UploadFile, File, Form           # noqa: E402
from fastapi.responses import (                               # noqa: E402
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
)
from pydantic import BaseModel                                # noqa: E402

from clipper.planner import (                                 # noqa: E402
    HOT_VIEWS, SALE_VIEWS, auto_clip_count, account_health, build_schedule, distribute,
    payout_for_views, days_left_to_payout, buster_earnings,
)
from clipper.publishers import youtube as yt                  # noqa: E402
from clipper.publishers import gdrive as gd                   # noqa: E402
from packages.integrations.telegram_bot import _call, _send_video   # noqa: E402

YT_REDIRECT = "http://localhost:8002/auth/yt/callback"
STATS_PULL_EVERY_S = 4 * 3600     # auto-pull YouTube stats every 4 hours
MAX_PUBLISH_ATTEMPTS = 6          # серий аплоада до фолбэка в ручной режим (~2.5ч)

DATA_DIR = CLIPPER_DIR / "data"
STATE_PATH = DATA_DIR / "state.json"
SOURCES_DIR = CLIPPER_DIR / "sources"
OUTPUT_DIR = CLIPPER_DIR / "output" / "jobs"
MUSIC_DIR = CLIPPER_DIR.parent / "assets" / "music"   # royalty-free фон для клипов
MUSIC_CATS = ("common", "films", "series", "videos", "buster")
MUSIC_EXTS = (".mp3", ".m4a", ".aac", ".wav", ".opus", ".ogg")
SETTINGS_PATH = REPO / "data" / "settings.json"

CATEGORIES = [("videos", "Видосы"), ("films", "Фильмы"), ("series", "Сериалы"),
              ("buster", "Бустер"), ("trezzy", "💎 TREZZY")]
CAT_FOLDER = {"videos": "видосы", "films": "фильмы", "series": "сериалы",
              "buster": "бустер", "trezzy": "trezzy"}
BUSTER_CAT = "buster"   # клип-программа стримера: своя мета, лимит аккаунтов, выплаты
TREZZY_CAT = "trezzy"   # партнёрка парфюм/косметика → trezzy.ru (CTA на видео + в описании)
TREZZY_SITE = "trezzy.ru"
TREZZY_AFF = "https://trezzy.ru/?affiliate_code=lg-MNglW2-IrgVj01aGmp"
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

_LOCK = threading.RLock()
_STATE_CACHE: Optional[Dict[str, Any]] = None
_CANCEL: set = set()        # id задач на отмену — воркер их прибирает (исходник + темп)


# ----------------------------------------------------------------------------
# State + settings
# ----------------------------------------------------------------------------
def _default_state() -> Dict[str, Any]:
    return {"accounts": [], "queue": [], "clips": {}, "plan": [],
            "health_warned": {}, "tg_offset": 0, "pending_input": None}


def _load_state() -> Dict[str, Any]:
    global _STATE_CACHE
    with _LOCK:
        if _STATE_CACHE is None:
            try:
                _STATE_CACHE = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            except Exception:
                _STATE_CACHE = _default_state()
        return _STATE_CACHE


def _save_state() -> None:
    with _LOCK:
        if _STATE_CACHE is None:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_STATE_CACHE, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_PATH)


def _settings() -> Dict[str, Any]:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _tg_creds() -> tuple:
    s = _settings()
    return (s.get("telegram_bot_token") or "", str(s.get("telegram_chat_id") or ""))


def _yt_redirect() -> str:
    """OAuth redirect URI. Если в settings задан public_base_url (HTTPS-домен сервера) —
    Google вернёт код прямо на наш /auth/yt/callback, БЕЗ ручного копирования. Иначе
    откатываемся на loopback localhost:8002 (старое поведение)."""
    base = (_settings().get("public_base_url") or "").strip().rstrip("/")
    return f"{base}/auth/yt/callback" if base else YT_REDIRECT


def _now_hm() -> str:
    return datetime.now().strftime("%H:%M")


def _notify(text: str) -> None:
    """Сообщение владельцу. Сеть до Telegram нестабильна — до 3 попыток."""
    token, chat = _tg_creds()
    if not (token and chat):
        return
    for attempt in range(1, 4):
        try:
            _call(token, "sendMessage", {"chat_id": chat, "text": text[:3900]})
            return
        except Exception as e:
            if attempt < 3:
                time.sleep(5 * attempt)
            else:
                print("[clipper] notify failed (3 попытки):", repr(e))


def _notify_kb(text: str, keyboard: Dict[str, Any]) -> bool:
    """Сообщение с inline-кнопками владельцу, до 3 попыток. True — доставлено.
    Нужно для важных уведомлений с кнопкой (напр. «📤 Сдать»): флаг 'уведомлён'
    ставится ТОЛЬКО при успехе, иначе сообщение терялось при первом сбое сети."""
    token, chat = _tg_creds()
    if not (token and chat):
        return False
    for attempt in range(1, 4):
        try:
            _call(token, "sendMessage", {"chat_id": chat, "text": text[:3900],
                                         "reply_markup": keyboard})
            return True
        except Exception as e:
            if attempt < 3:
                time.sleep(5 * attempt)
            else:
                print("[clipper] notify(kb) failed (3 попытки):", repr(e))
    return False


def _send_video_retry(token: str, chat: str, video: Path, caption: str, kb) -> bool:
    """Отправка видео в TG с 3 попытками (SSL-таймауты — обычное дело)."""
    for attempt in range(1, 4):
        try:
            _send_video(token, chat, video, caption, kb)
            return True
        except Exception as e:
            if attempt < 3:
                print(f"[clipper] TG видео: попытка {attempt} сорвалась ({str(e)[:80]}), повтор...", flush=True)
                time.sleep(8 * attempt)
            else:
                print("[clipper] TG send failed (3 попытки):", repr(e))
    return False


# ── Красивые метаданные публикации ──────────────────────────────────────────
DEFAULT_TAGS = {
    "films":  ["#shorts", "#фильм", "#кино", "#моменты", "#нарезка"],
    "series": ["#shorts", "#сериал", "#моменты", "#лучшее"],
    "videos": ["#shorts", "#вирусное", "#моменты"],
    "buster": ["#buster", "#shorts", "#twitch", "#стрим", "#моменты"],
    "trezzy": ["#trezzy", "#shorts"],
}
# Пул TREZZY-хештегов — на каждый клип берём РАЗНЫЙ, но СТАБИЛЬНЫЙ набор
# (#trezzy + #shorts всегда). Парфюм/косметика/бьюти-тематика.
TREZZY_HASHTAG_POOL = [
    "#косметика", "#парфюм", "#парфюмерия", "#аромат", "#духи", "#нишеваяпарфюмерия",
    "#бьюти", "#beauty", "#уходзакожей", "#макияж", "#красота", "#perfume",
    "#fragrance", "#ароматы", "#парфюммания", "#обзор", "#люкс", "#тренд",
]


def _polish_meta(clip: Dict[str, Any]) -> Dict[str, Any]:
    """Аккуратные название/описание/хештеги для публикации.

    Берём то, что сгенерил LLM при нарезке (yt_title/yt_desc/tags); если его не
    было (сеть лежала) — детерминированно чистим: убираем «момент N», строим
    заголовок из хука, добавляем дефолтные хештеги категории.
    """
    tags = clip.get("tags") or DEFAULT_TAGS.get(clip.get("category", ""), ["#shorts"])
    tags = [t if t.startswith("#") else f"#{t}" for t in tags]
    # #БУСТЕРРОФЛС/#BUSTERROFLS (имя стримера — LLM добавлял сам) убираем ВООБЩЕ.
    # На #buster для выплат это НЕ влияет (он добавляется отдельно ниже).
    tags = [t for t in tags
            if "бустеррофл" not in t.lower() and "busterrofl" not in t.lower()][:6]

    title = (clip.get("yt_title") or clip.get("title") or "").strip()
    hook = (clip.get("hook") or "").strip()
    if not title or re.search(r"момент\s*\d+\s*$", title, re.IGNORECASE):
        if hook:
            words = hook.split()
            title = " ".join(words[:8]) + ("…" if len(words) > 8 else "")
        else:
            title = Path(str(clip.get("source") or "Shorts")).stem.replace("_", " ").strip()
    title = re.sub(r"\s+", " ", title).strip(" .,—-")
    if title:
        title = title[0].upper() + title[1:]
    title = title[:90]

    desc = (clip.get("yt_desc") or "").strip()
    if not desc:
        desc = hook if hook else title

    # Buster-вертикаль: программа требует #buster + twitch-канал, причём для
    # YouTube — В ЗАГОЛОВКЕ шортса. Вшиваем ОБА здесь — единая точка, через которую
    # идут и автопост, и TG-карточка одобрения; работает даже без LLM-полировки.
    if clip.get("category") == BUSTER_CAT:
        bs = _settings()
        htag = (bs.get("buster_hashtag") or "#buster").strip()
        htag = htag if htag.startswith("#") else f"#{htag}"
        chan = (bs.get("buster_channel_url") or "twitch.tv/buster").strip()
        # заголовок: оставляем место под автодобавляемый #Shorts (лимит ~95)
        base = title[:55].rstrip(" .,—-")
        suff = [x for x in (chan, htag) if x.lower() not in base.lower()]
        title = (base + ((" " + " ".join(suff)) if suff else "")).strip()
        # #buster в список хештегов (snippet.tags + строка хештегов в описании)
        if htag.lower() not in [t.lower() for t in tags]:
            tags = [htag] + tags
        tags = tags[:6]
        # описание ведём с twitch-канала (как в примере правил программы)
        if chan.lower() not in desc.lower():
            desc = (f"📺 Стрим: {chan}\n\n" + desc).strip()

    # TREZZY-вертикаль: парфюм/косметика. РАЗНЫЕ (но стабильные на клип) хештеги +
    # CTA в описании с партнёрской ссылкой. Текстовый CTA на самом видео рисует рендер.
    if clip.get("category") == TREZZY_CAT:
        seed = f"{clip.get('id','')}|{clip.get('title','')}|{clip.get('duration','')}"
        pool = TREZZY_HASHTAG_POOL[:]
        random.Random(seed).shuffle(pool)
        tags = (["#trezzy", "#shorts"] + pool[:4])[:6]
        desc = (f"{desc}\n\n🛍 Купить лучший парфюм и косметику — {TREZZY_SITE}\n"
                f"🔗 Ссылка в шапке профиля → {TREZZY_AFF}").strip()

    desc = f"{desc}\n\n{' '.join(tags)}"[:4500]
    return {"title": title or "Shorts", "description": desc, "hashtags": tags}


def _clip_text(transcript: Dict[str, Any], start: float, end: float, limit: int = 400) -> str:
    """Реальный текст клипа из транскрипта (сегменты в окне [start,end]) — чтобы LLM
    придумывал название по СУТИ клипа, а не по одному короткому хуку."""
    parts: List[str] = []
    for s in (transcript or {}).get("segments") or []:
        try:
            if float(s.get("end", 0)) <= start or float(s.get("start", 0)) >= end:
                continue
        except Exception:
            continue
        t = (s.get("text") or "").strip()
        if t:
            parts.append(t)
    return " ".join(parts)[:limit]


def _pick_llm_provider(settings: Dict[str, Any]) -> tuple:
    """Выбрать LLM-провайдера для отбора моментов и названий. Порядок: anthropic →
    openai → GROQ → gemini (Groq впереди Gemini — тот цензурит контент бустера).
    Учитывает И одиночный ключ, И СПИСОК (groq_api_keys/gemini_api_keys) — иначе при
    заполненном только списке провайдер молча пропускался и отбор валился в fill.
    Реальную ротацию по пулу делают _post_groq/_post_gemini, тут нужен любой 1 ключ."""
    pairs = (("anthropic", "anthropic_api_key", None),
             ("openai", "openai_api_key", None),
             ("groq", "groq_api_key", "groq_api_keys"),
             ("gemini", "gemini_api_key", "gemini_api_keys"))
    for prov, single, lst in pairs:
        key = settings.get(single)
        key = key.strip() if isinstance(key, str) else key
        if not key and lst:
            arr = settings.get(lst) or []
            if isinstance(arr, str):
                arr = arr.split(",")
            arr = [str(x).strip() for x in arr if str(x).strip()]
            if arr:
                key = arr[0]
        if key:
            return prov, key
    return None, None


def _llm_polish_clips(clips: List[Dict[str, Any]], source_name: str, cat_label: str,
                      settings: Dict[str, Any],
                      src_desc: str = "") -> Optional[List[Dict[str, Any]]]:
    """Один запрос к LLM: цепляющие названия/описания/хештеги для всех клипов
    видео. None при любой ошибке — тогда работает детерминированный фолбэк."""
    prov, key = _pick_llm_provider(settings)
    if not prov:
        return None
    try:
        from packages.agents import llm_client
        lines = "\n".join(
            f"{i + 1}. хук: {(c.get('hook') or '—')[:80]}\n"
            f"   текст клипа: {(c.get('text') or c.get('title') or '—')[:380]}"
            for i, c in enumerate(clips))
        system = (
            "Ты — топовый редактор вирусных YouTube Shorts на русском. По РЕАЛЬНОМУ тексту "
            "каждого клипа придумай НАЗВАНИЕ, на которое невозможно не кликнуть.\n"
            "Под КАЖДЫЙ клип сам выбери приём, который цепляет именно в этом моменте — "
            "где-то интрига, где-то конкретный факт/ставка, где-то эмоция; не лепи один шаблон на все.\n"
            "Что делает название Shorts вирусным:\n"
            "— цепляет за секунду: интрига, эмоция, разрыв шаблона или недосказанность («и тут он…»);\n"
            "— конкретика и ставка (что на кону), а НЕ общие слова («финальный матч» — слабо);\n"
            "— живой разговорный язык блогеров, без канцелярита, без кавычек;\n"
            "— до 80 символов, по-русски;\n"
            "— НИКАКИХ выдумок: только то, что реально звучит в тексте клипа.\n"
            "Примеры сильных: «поставил всё на один бросок», «не ожидал такого в чате», "
            "«этот момент порвал стрим».\n"
            "Для каждого клипа: title, description (1-2 живых предложения), "
            "hashtags (4-6, первым #shorts; ПО ТЕМЕ клипа, НЕ имя стримера/канала). "
            "Отвечай ТОЛЬКО валидным JSON.")
        desc_block = f"\nОписание исходного ролика (используй как контекст):\n{src_desc[:700]}\n" \
            if src_desc else ""
        user = (f"Источник: {source_name} (категория: {cat_label}).{desc_block} "
                f"Клипов: {len(clips)} — верни РОВНО {len(clips)} items.\nКлипы:\n{lines}\n\n"
                'Ответь JSON: {"items": [{"n": 1, "title": "...", "description": "...", '
                '"hashtags": ["#shorts", "..."]}]}. Поле "n" — НОМЕР клипа из списка выше '
                '(1, 2, 3...), по элементу на КАЖДЫЙ клип, n обязан совпадать с номером клипа.')
        mdl = settings.get("gemini_model") if prov == "gemini" else None
        data = llm_client.complete_json(prov, key, system, user, max_tokens=2000,
                                        temperature=0.7, model=mdl)
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list) and items:
            # ВАЖНО: привязка по номеру n, а НЕ по позиции — LLM иногда возвращает
            # клипы в другом порядке, и заголовок попадал на ЧУЖОЙ ролик. Если n не
            # дали — оставляем пусто, _polish_meta строит заголовок из хука клипа.
            by_n: Dict[int, Dict[str, Any]] = {}
            for it in items:
                if not isinstance(it, dict):
                    continue
                try:
                    n = int(it.get("n"))
                except (TypeError, ValueError):
                    continue
                if 1 <= n <= len(clips) and n not in by_n:
                    by_n[n] = it           # первый валидный item на слот выигрывает
                else:
                    print(f"[clipper] полировка: пропущен item n={it.get('n')} "
                          f"(дубль/вне диапазона 1..{len(clips)})", flush=True)
            if not by_n and len(items) == len(clips):
                # n не дали, но кол-во совпало → по позиции (только dict-элементы)
                return [it if isinstance(it, dict) else {} for it in items]
            return [by_n.get(i + 1) or {} for i in range(len(clips))]
    except Exception as e:
        print("[clipper] полировка метаданных не удалась:", str(e)[:120])
    return None


# ----------------------------------------------------------------------------
# Sources
# ----------------------------------------------------------------------------
def _ensure_dirs() -> None:
    for _, folder in CAT_FOLDER.items():
        (SOURCES_DIR / folder).mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _list_sources() -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    st = _load_state()
    queued = {(q["category"], q["file"]) for q in st["queue"]
              if q["status"] in ("pending", "processing") and q.get("file")}
    for key, folder in CAT_FOLDER.items():
        items = []
        d = SOURCES_DIR / folder
        if d.exists():
            for p in sorted(d.iterdir()):
                if p.suffix.lower() in VIDEO_EXTS and p.is_file():
                    items.append({
                        "name": p.name,
                        "size_mb": round(p.stat().st_size / 1e6, 1),
                        "queued": (key, p.name) in queued,
                    })
        out[key] = items
    return out


# ----------------------------------------------------------------------------
# Batch worker: cut queued videos
# ----------------------------------------------------------------------------
def _process_video(item: Dict[str, Any]) -> None:
    from packages.video.transcribe import transcribe
    from packages.video.clip_renderer import render_clips, video_duration
    from packages.agents.clip_agent import ClipAgent
    from packages.agents.base import AgentContext

    settings = _settings()
    src = SOURCES_DIR / CAT_FOLDER[item["category"]] / item["file"]
    if not src.exists():
        raise RuntimeError(f"файл не найден: {src.name}")

    # Транскрипт — главный (и кэшируемый) шаг; делаем его первым, длительность
    # берём из него же, чтобы не гонять ffmpeg вторым проходом ради одной цифры.
    transcript = transcribe(src, settings)
    # Без распознанной речи выйдут клипы без субтитров и с пустыми названиями —
    # такой мусор не нарезаем, лучше честная ошибка и кнопка «Повторить».
    if settings.get("clip_burn_captions", True) and not transcript.get("segments"):
        err = str(transcript.get("error") or "речь не распозналась")[:120]
        raise RuntimeError(f"Субтитры не получились ({err}). Обычно это сбой сети до Groq — "
                           f"нажми «↻ Повторить» в очереди.")

    # Длину для СЧЁТА клипов берём из РЕАЛЬНОГО файла (ffmpeg-заголовок), а не из
    # транскрипта: на длинном видео Groq-распознавание частичное (квота) и его
    # "duration" = конец последнего распознанного слова → ролик кажется коротким и
    # клипов выходит мало. Транскрипт оставляем только для границ/субтитров.
    tr_dur = transcript.get("duration") or 0
    true_dur = video_duration(src) or tr_dur or 0
    per10 = _load_state().get("clips_per_10min") or settings.get("clips_per_10min") or 3
    n_clips = auto_clip_count(true_dur, per10)
    print(f"[clipper] {src.name}: файл ~{int(true_dur / 60)} мин (распознано ~{int(tr_dur / 60)} мин, "
          f"сегментов {len(transcript.get('segments') or [])}) → целюсь в {n_clips} клипов", flush=True)
    dur = true_dur
    prov, key = _pick_llm_provider(settings)
    # Если источник скачан с YouTube — у нас есть его настоящее название.
    topic = (item.get("src_title") or src.stem).strip()
    ctx = AgentContext(topic=topic, format="clip")
    sel = ClipAgent(llm_provider=prov, llm_key=key).run(
        ctx, transcript=transcript, source_duration=dur, target_count=n_clips)
    moments = sel.get("moments", [])
    print(f"[clipper] выбрано моментов: {len(moments)} из целевых {n_clips}", flush=True)
    if not moments:
        raise RuntimeError("не выбрано ни одного момента")

    job_id = f"{item['category']}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:5]}"
    job_dir = OUTPUT_DIR / job_id
    # Музыка решается ГАЛОЧКОЙ при скачивании (item["music"]), а не глобально:
    # отмечена → у клипов этой закачки фон, нет → без музыки. Громкость — из state
    # (рулится из дашборда), трек подбирается по настроению клипа в рендере.
    render_settings = {**settings,
                       "bg_music_enabled": bool(item.get("music")),
                       "bg_music_volume": float(_load_state().get(
                           "bg_music_volume", settings.get("bg_music_volume", 0.12)) or 0.12)}
    res = render_clips(src, moments, transcript, job_dir, render_settings,
                       meta={"topic": src.stem, "hashtags": [], "category": item["category"]},
                       should_cancel=lambda: item.get("id") in _CANCEL)
    if item.get("id") in _CANCEL:               # отменили во время рендера → бросаем,
        raise RuntimeError("отменено пользователем")   # воркер вызовет _finish_cancel

    token, chat = _tg_creds()
    st = _load_state()
    cat_label = dict(CATEGORIES)[item["category"]]
    clips_out = res.get("clips", [])
    print(f"[clipper] нарезано клипов: {len(clips_out)} (из {len(moments)} моментов)", flush=True)

    # Реальный текст каждого клипа → LLM делает название по СУТИ, а не по тонкому хуку.
    for c in clips_out:
        c["text"] = _clip_text(transcript, c.get("start") or 0, c.get("end") or 0)
    # Один LLM-проход: цепляющие названия/описания/хештеги для всех клипов сразу.
    polished = _llm_polish_clips(clips_out, topic, cat_label, settings,
                                 src_desc=item.get("src_desc") or "")

    for idx, c in enumerate(clips_out):
        clip_id = f"{job_id}-c{c['index']:02d}"
        meta_llm = (polished[idx] if polished else {}) or {}
        with _LOCK:
            st["clips"][clip_id] = {
                "id": clip_id, "job_id": job_id, "category": item["category"],
                "path": c["path"], "title": c.get("title") or src.stem,
                "score": c.get("score"), "hook": c.get("hook") or "",
                "duration": c.get("duration"), "source": src.name,
                "yt_title": str(meta_llm.get("title") or "").strip()[:90],
                "yt_desc": str(meta_llm.get("description") or "").strip()[:1000],
                "tags": [str(t).strip() for t in (meta_llm.get("hashtags") or []) if str(t).strip()][:6],
                "status": "review" if (token and chat) else "approved",
                "views": None, "likes": None, "hot_notified": False,
                "created": datetime.now().isoformat(timespec="seconds"),
            }
            _save_state()
        if token and chat:
            meta = _polish_meta(st["clips"][clip_id])
            cap = (f"🎬 {cat_label} | {src.name}\n"
                   f"⭐ Балл: {c.get('score', '—')} | ⏱ {c.get('duration')}с\n\n"
                   f"📌 {meta['title']}\n"
                   f"{' '.join(meta['hashtags'])}\n\nПубликуем?")
            kb = {"inline_keyboard": [[
                {"text": "✅ Одобрить", "callback_data": f"c:ok:{clip_id}"},
                {"text": "❌ Мимо", "callback_data": f"c:no:{clip_id}"},
            ]]}
            _send_video_retry(token, chat, Path(c["path"]), cap, kb)
    item["clips"] = len(clips_out)
    # Авто-очистка: исходник уже нарезан — удаляем (диск маленький, клипы лежат
    # отдельно в output/jobs/). С YouTube перекачаем при надобности.
    if clips_out:
        try:
            src.unlink(missing_ok=True)
            print(f"[clipper] исходник удалён после нарезки: {src.name}", flush=True)
        except Exception as e:
            print(f"[clipper] не смог удалить исходник: {str(e)[:80]}", flush=True)


def _finish_cancel(job: Dict[str, Any]) -> None:
    """Прибрать отменённую задачу: удалить недокачанный/нарезанный исходник и саму
    запись из очереди. Темп воркеров чистится их собственными finally."""
    try:
        f = job.get("file")
        if f:
            (SOURCES_DIR / CAT_FOLDER[job["category"]] / f).unlink(missing_ok=True)
    except Exception:
        pass
    with _LOCK:
        st = _load_state()
        st["queue"] = [q for q in st["queue"] if q.get("id") != job.get("id")]
        _CANCEL.discard(job.get("id"))
        _save_state()


def _worker_loop() -> None:
    while True:
        st = _load_state()
        job = None
        with _LOCK:
            for q in st["queue"]:
                if q["status"] == "pending":
                    q["status"] = "processing"
                    job = q
                    _save_state()
                    break
        if not job:
            time.sleep(3)
            continue
        try:
            if job["id"] in _CANCEL:
                _finish_cancel(job)
                continue
            # Источник — ссылка YouTube: сначала качаем (лучшее ≤1080p),
            # описание ролика станет контекстом для названий шортсов.
            if job.get("url") and not job.get("downloaded"):
                from clipper.downloader import download_youtube
                print(f"[clipper] качаю с YouTube: {job['url']}", flush=True)
                dl = download_youtube(job["url"], SOURCES_DIR / CAT_FOLDER[job["category"]],
                                      settings=_settings(),
                                      should_cancel=lambda jid=job["id"]: jid in _CANCEL)
                with _LOCK:
                    job["file"] = dl["file"]
                    job["src_title"] = dl["title"]
                    job["src_desc"] = dl["description"]
                    job["downloaded"] = True
                    _save_state()
                print(f"[clipper] скачано: {dl['file']} ({dl.get('duration')}с)", flush=True)
            if job["id"] in _CANCEL:               # отмена между скачиванием и нарезкой
                _finish_cancel(job)
                continue
            _process_video(job)
            with _LOCK:
                job["status"] = "done"
                _save_state()
            print(f"[clipper] {job['file']}: готово, клипов: {job.get('clips')}", flush=True)
        except Exception as e:
            if job["id"] in _CANCEL:               # упало из-за отмены — это не ошибка
                _finish_cancel(job)
                print(f"[clipper] задача отменена: {job.get('file') or job.get('url')}", flush=True)
                continue
            with _LOCK:
                job["status"] = "failed"
                job["error"] = str(e)[:300]
                _save_state()
            print(f"[clipper] {job.get('file') or job.get('url')}: ОШИБКА {e!r}", flush=True)


# ----------------------------------------------------------------------------
# Telegram poller: approvals, posting confirmations, stats entry
# ----------------------------------------------------------------------------
def _approved_dir(category: str) -> Path:
    d = CLIPPER_DIR / "output" / "approved" / CAT_FOLDER[category]
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Автопакет выплаты (buster): заливка записи статистики на Drive + готовый текст ──
def _tg_download_file(token: str, file_id: str, dest: Path) -> bool:
    """Скачать присланный в бот файл (видео/документ) по file_id."""
    try:
        info = _call(token, "getFile", {"file_id": file_id})
        fp = (info.get("result") or {}).get("file_path")
        if not fp:
            return False
        url = f"https://api.telegram.org/file/bot{token}/{fp}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=240) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
        return dest.exists() and dest.stat().st_size > 0
    except Exception as e:
        print(f"[buster] не скачал файл из TG: {str(e)[:100]}", flush=True)
        return False


def _buster_packet(clip: Dict[str, Any], acc: Dict[str, Any], drive_link: str, vid: str = "") -> str:
    """Готовый текст для формы выплаты (копипаст по полям)."""
    s = _settings()
    video_link = f"https://youtu.be/{vid}" if vid else "—"
    v = clip.get("views")
    views = f"{v:,}".replace(",", " ") if isinstance(v, int) else "—"
    payout = payout_for_views(v if isinstance(v, int) else 0, s.get("buster_payout_table") or [])
    cur = s.get("buster_payout_currency") or "$"
    wallet = acc.get("payout_wallet") or "⚠ укажи USDT-адрес в карточке аккаунта (дашборд)"
    login = acc.get("login") or acc.get("name") or "—"
    my_tg = s.get("buster_my_tg") or "⚠ впиши свой TG в settings.json (buster_my_tg)"
    form = s.get("buster_payout_form_url") or ""
    return (
        "📦 ПАКЕТ ДЛЯ ФОРМЫ ВЫПЛАТЫ — скопируй по полям:\n\n"
        f"Ваш TG: {my_tg}\n"
        f"Ссылка на видео: {video_link}\n"
        f"Кол-во просмотров: {views}\n"
        f"ОПЛАТА (TRC-20 USDT): {wallet}\n"
        f"Ник аккаунта (логин): {login}\n"
        f"Google Drive (запись статистики): {drive_link}\n\n"
        f"💵 Сумма по таблице: {cur}{payout}\n"
        f"📝 Форма: {form}\n"
        "После отправки формы жми «✅ сдал» под клипом в дашборде."
    )


def _buster_finish(clip_id: str, file_id: str, token: str, chat: str) -> None:
    """Получили запись статистики → грузим на Drive аккаунта канала → готовый пакет."""
    st = _load_state()
    clip = st["clips"].get(clip_id) or {}
    p = next((x for x in st["plan"] if x.get("clip_id") == clip_id and x.get("video_id")), None)
    acc = next((a for a in st["accounts"] if a["id"] == (p or {}).get("account_id")), None) if p else None
    if acc is None:   # фолбэк: любой подключённый buster-аккаунт
        acc = next((a for a in st["accounts"] if a.get("category") == BUSTER_CAT and _yt_ready(a)), None)
    if not acc or not _yt_ready(acc):
        _call(token, "sendMessage", {"chat_id": chat,
              "text": "⚠ Не нашёл подключённый YouTube-аккаунт для этого клипа — подключи канал в дашборде."})
        return
    _call(token, "sendMessage", {"chat_id": chat, "text": "⏳ Заливаю запись на Google Drive…"})
    tmp = DATA_DIR / f"_buster_{clip_id}.mp4"
    try:
        if not _tg_download_file(token, file_id, tmp):
            _call(token, "sendMessage", {"chat_id": chat, "text": "⚠ Не смог скачать запись из Telegram — пришли ещё раз."})
            return
        y = acc["yt"]
        access = yt.refresh_access_token(y["client_id"], y["client_secret"], y["refresh_token"])
        name = f"stats_{acc.get('login') or acc.get('name')}_{clip_id}.mp4"
        fid = gd.upload_file(access, tmp, name)
        gd.make_public(access, fid)
        link = gd.file_link(fid)
        with _LOCK:
            st["pending_input"] = None
            clip["buster_drive_link"] = link
            _save_state()
        _call(token, "sendMessage", {"chat_id": chat,
              "text": _buster_packet(clip, acc, link, vid=(p or {}).get("video_id", ""))})
    except gd.DriveError as e:
        _call(token, "sendMessage", {"chat_id": chat,
              "text": f"⚠ Drive отказал: {str(e)[:140]}\nСкорее всего нужно переподключить канал "
                      f"(добавить Drive-доступ): дашборд → «🔗 Подключить»."})
    except yt.YouTubeAuthError:
        _call(token, "sendMessage", {"chat_id": chat,
              "text": "🔑 Токен канала протух — переподключи в дашборде и пришли запись снова."})
    except Exception as e:
        _call(token, "sendMessage", {"chat_id": chat, "text": f"⚠ Сбой заливки: {str(e)[:140]}"})
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _handle_callback(cb: Dict[str, Any], token: str, chat: str) -> None:
    data = str(cb.get("data") or "")
    cb_id = cb.get("id")
    st = _load_state()
    answer = ""

    if data.startswith("c:ok:") or data.startswith("c:no:"):
        clip_id = data.split(":", 2)[2]
        do_copy = None
        with _LOCK:        # claim статуса review→approved/rejected атомарно (без задвоения)
            clip = st["clips"].get(clip_id)
            if clip and clip["status"] == "review":
                if data.startswith("c:ok:"):
                    clip["status"] = "approved"
                    do_copy = (clip.get("path"), clip.get("category"))
                    answer = "✅ В очередь публикаций"
                else:
                    clip["status"] = "rejected"
                    answer = "❌ Отклонён"
                _save_state()
            else:
                # уже обработан ранее ИЛИ дубль callback'а — показываем реальный статус,
                # а не пугающее «Уже обработан» (клип мог давно одобриться/уйти в план).
                if clip and clip.get("status") == "approved":
                    sched = next((p for p in st["plan"]
                                  if p.get("clip_id") == clip_id and p.get("status") == "scheduled"), None)
                    answer = (f"✅ Уже одобрен — в плане {sched.get('date')} {sched.get('slot')}"
                              if sched else "✅ Уже одобрен (в план встанет, как будет аккаунт под категорию)")
                elif clip and clip.get("status") == "rejected":
                    answer = "❌ Уже отклонён"
                else:
                    answer = "⚠️ Карточка устарела — клип не найден (старый сервер/пере-нарезка)"
        if do_copy and do_copy[0]:        # копируем ВНЕ лока (IO не держит другие потоки)
            try:
                dst = _approved_dir(do_copy[1]) / Path(do_copy[0]).name
                shutil.copyfile(do_copy[0], dst)
            except Exception:
                pass
        if do_copy:                       # клип одобрен → АВТО-сборка плана (без ручной кнопки)
            try:
                _build_plan()
                sched = next((p for p in _load_state()["plan"]
                              if p.get("clip_id") == clip_id and p.get("status") == "scheduled"), None)
                if sched:
                    answer = f"✅ В плане: {sched.get('date')} в {sched.get('slot')}"
            except Exception as e:
                print("[clipper] авто-план после одобрения не удался:", repr(e), flush=True)

    elif data.startswith("p:done:") or data.startswith("p:skip:"):
        pid = data.split(":", 2)[2]
        entry = next((p for p in st["plan"] if p["id"] == pid), None)
        if entry:
            with _LOCK:
                entry["status"] = "posted" if data.startswith("p:done:") else "skipped"
                entry["marked_at"] = datetime.now().isoformat(timespec="seconds")
                if entry["status"] == "posted":
                    entry.setdefault("posted_at", entry["marked_at"])  # якорь окна выплаты
                _save_state()
            answer = "🟢 Отмечено: опубликован" if data.startswith("p:done:") else "⏭ Пропущен"

    elif data.startswith("st:acc:"):
        acc_id = data.split(":", 2)[2]
        acc = next((a for a in st["accounts"] if a["id"] == acc_id), None)
        if acc:
            posted = [p for p in st["plan"] if p["account_id"] == acc_id and p["status"] == "posted"]
            lines = [f"📊 {acc['name']} ({dict(CATEGORIES).get(acc['category'], '')})",
                     f"Опубликовано: {len(posted)}"]
            buttons = []
            total_views = 0
            for p in posted[-10:]:
                clip = st["clips"].get(p["clip_id"]) or {}
                v = clip.get("views")
                total_views += v or 0
                label = f"{(clip.get('title') or p['clip_id'])[:28]} — {v if v is not None else '?'} 👁"
                buttons.append([{"text": label, "callback_data": f"st:clip:{p['clip_id']}"}])
            lines.append(f"Просмотров (введено): {total_views}")
            lines.append("Нажми ролик, чтобы ввести/обновить просмотры:")
            payload = {"chat_id": chat, "text": "\n".join(lines)}
            if buttons:
                payload["reply_markup"] = {"inline_keyboard": buttons}
            _call(token, "sendMessage", payload)
            answer = ""

    elif data.startswith("st:clip:"):
        clip_id = data.split(":", 2)[2]
        with _LOCK:
            st["pending_input"] = {"type": "views", "clip_id": clip_id}
            _save_state()
        clip = st["clips"].get(clip_id) or {}
        _call(token, "sendMessage", {"chat_id": chat,
              "text": f"Пришли число просмотров для «{(clip.get('title') or clip_id)[:60]}»\n"
                      f"(можно «12500» или «12500 800» = просмотры лайки)"})
        answer = ""

    elif data.startswith("b:sub:"):
        clip_id = data.split(":", 2)[2]
        with _LOCK:
            st["pending_input"] = {"type": "buster_video", "clip_id": clip_id}
            _save_state()
        clip = st["clips"].get(clip_id) or {}
        _call(token, "sendMessage", {"chat_id": chat,
              "text": f"📤 Сдача «{(clip.get('title') or clip_id)[:50]}»:\n"
                      f"1) Открой YouTube Studio → статистику этого ролика\n"
                      f"2) Запиши экран (видно просмотры + обнови страницу — докажи, что аккаунт твой)\n"
                      f"3) Пришли эту запись СЮДА — видео или файлом.\n\n"
                      f"Я залью её на Google Drive нужного аккаунта и соберу готовый пакет для формы."})
        answer = "📤 Жду запись статистики"

    if cb_id:
        try:
            _call(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": answer[:180]})
        except Exception:
            pass


def _handle_message(msg: Dict[str, Any], token: str, chat: str) -> None:
    text = str(msg.get("text") or "").strip()
    st = _load_state()

    pending = st.get("pending_input")
    if pending and pending.get("type") == "buster_video":
        media = msg.get("video") or msg.get("document") or msg.get("animation")
        file_id = (media or {}).get("file_id")
        if file_id:
            _buster_finish(pending.get("clip_id"), file_id, token, chat)
            return
        if text.lower() in ("/start", "отмена", "/cancel"):
            with _LOCK:
                st["pending_input"] = None
                _save_state()
            _call(token, "sendMessage", {"chat_id": chat, "text": "Ок, отменил сдачу."})
            return
        _call(token, "sendMessage", {"chat_id": chat,
              "text": "Пришли именно ВИДЕО-запись статистики (видео или файлом). Отмена — «отмена»."})
        return
    if pending and pending.get("type") == "views":
        parts = text.replace(",", " ").split()
        if parts and parts[0].isdigit():
            views = int(parts[0])
            likes = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            clip = st["clips"].get(pending["clip_id"])
            with _LOCK:
                if clip is not None:
                    clip["views"] = views
                    if likes is not None:
                        clip["likes"] = likes
                st["pending_input"] = None
                _save_state()
            _call(token, "sendMessage", {"chat_id": chat,
                  "text": f"Записал: {views} 👁" + (f", {likes} ❤" if likes is not None else "")})
            _check_hot_clips()
            return
        with _LOCK:
            st["pending_input"] = None
            _save_state()

    if text.lower() in ("/start", "/stats", "статистика", "📊", "📊 статистика"):
        buttons = [[{"text": f"{a['name']} ({dict(CATEGORIES).get(a['category'], '')})",
                     "callback_data": f"st:acc:{a['id']}"}] for a in st["accounts"]]
        payload = {"chat_id": chat,
                   "text": "Нарезчик на связи. Выбери аккаунт для статистики:" if buttons
                           else "Нарезчик на связи. Аккаунтов пока нет — добавь в дашборде (порт 8002)."}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        _call(token, "sendMessage", payload)


def _tg_poller_loop() -> None:
    while True:
        token, chat = _tg_creds()
        if not (token and chat):
            time.sleep(20)
            continue
        st = _load_state()
        try:
            res = _call(token, "getUpdates",
                        {"offset": int(st.get("tg_offset") or 0) + 1, "timeout": 25,
                         "allowed_updates": ["message", "callback_query"]}, timeout=35)
            for upd in res.get("result") or []:
                cb = upd.get("callback_query")
                msg = upd.get("message")
                from_id = str(((cb or msg or {}).get("from") or {}).get("id") or "")
                try:
                    if from_id == str(chat):   # only the owner
                        if cb:
                            _handle_callback(cb, token, chat)
                        elif msg:
                            _handle_message(msg, token, chat)
                except Exception as he:
                    print("[clipper] TG handler error:", repr(he))
                # offset подтверждаем ПОСЛЕ обработки (а не до): краш/перезапуск
                # между обработкой и подтверждением больше не теряет нажатие ✅/❌ —
                # Telegram переотдаст апдейт, обработчики идемпотентны по статусам.
                with _LOCK:
                    st["tg_offset"] = max(int(st.get("tg_offset") or 0), int(upd["update_id"]))
                    _save_state()
        except Exception as e:
            s = str(e)
            if "409" in s:
                print("[clipper] TG: другой поллер активен (завод?). Жду 30с...", flush=True)
                time.sleep(30)
            else:
                print("[clipper] TG poll error:", s[:160])
                time.sleep(10)


# ----------------------------------------------------------------------------
# Scheduler: due posts, hot clips, account health
# ----------------------------------------------------------------------------
def _check_hot_clips() -> None:
    st = _load_state()
    with _LOCK:                    # снимки: коллекции мутируют воркер/автопост на лету
        plan_snapshot = list(st["plan"])
        clips_snapshot = list(st["clips"].values())
        acc_by_id = {a["id"]: a for a in st["accounts"]}
    clip_acc = {}
    clip_posted_at = {}
    for p in plan_snapshot:
        if p.get("status") == "posted":
            clip_acc[p["clip_id"]] = acc_by_id.get(p["account_id"], {})
            clip_posted_at[p["clip_id"]] = p.get("posted_at") or p.get("marked_at")
    # Настройки Buster для напоминаний о выплате (читаются один раз).
    bs = _settings()
    b_table = bs.get("buster_payout_table") or []
    b_window = int(bs.get("buster_payout_window_days", 14) or 14)
    b_cur = bs.get("buster_payout_currency") or "$"
    b_form = bs.get("buster_payout_form_url") or ""
    b_contact = bs.get("buster_payout_contact") or ""
    now = datetime.now()
    for clip in clips_snapshot:
        v = clip.get("views")
        if not isinstance(v, int) or isinstance(v, bool):
            continue
        views_str = f"{v:,}".replace(",", " ")
        if v >= HOT_VIEWS and not clip.get("hot_notified"):
            with _LOCK:
                clip["hot_notified"] = True
                _save_state()
            _notify(f"🚀 РОЛИК СТРЕЛЯЕТ!\n«{(clip.get('title') or '')[:80]}»\n"
                    f"{views_str} просмотров — задумайся о дубле/продолжении!")
        # Бизнес-сигнал владельца: 400k+ = канал созрел для продажи.
        if v >= SALE_VIEWS and not clip.get("sale_notified"):
            with _LOCK:
                clip["sale_notified"] = True
                _save_state()
            acc_name = clip_acc.get(clip["id"], {}).get("name", "?")
            _notify(f"💰 КАНАЛ ГОТОВ К ПРОДАЖЕ!\n"
                    f"Аккаунт: {acc_name}\n«{(clip.get('title') or '')[:80]}»\n"
                    f"{views_str} просмотров — залёт случился, можно выставлять. "
                    f"Перед продажей: чистые страйки, включённые расширенные функции "
                    f"поднимают цену.")

        # Buster: напомнить сдать ролик на выплату, пока окно (14 дней) открыто.
        if clip.get("category") == BUSTER_CAT and not clip.get("buster_submitted"):
            payout = payout_for_views(v, b_table)
            if payout > 0:
                days_left = days_left_to_payout(clip_posted_at.get(clip["id"]), b_window, now)
                window_open = (days_left is None) or (days_left >= 0)
                if window_open and not clip.get("buster_payout_notified"):
                    dl_txt = f"{days_left} дн" if isinstance(days_left, int) else "—"
                    body = (f"💵 БУСТЕР: ролик набрал {views_str} → выплата {b_cur}{payout}!\n"
                            f"«{(clip.get('title') or '')[:70]}»\n"
                            f"Окно сдачи: {dl_txt}. Вопросы: {b_contact}\n"
                            f"Жми «📤 Сдать» — соберу пакет и залью запись на Drive сам.")
                    kb = {"inline_keyboard": [[
                        {"text": "📤 Сдать на выплату", "callback_data": f"b:sub:{clip['id']}"}]]}
                    # флаг ставим ТОЛЬКО после успешной доставки (с ретраями), иначе это
                    # единственное уведомление с кнопкой терялось при первом сбое сети.
                    if _notify_kb(body, kb):
                        with _LOCK:
                            clip["buster_payout_notified"] = True
                            _save_state()
                if (isinstance(days_left, int) and 0 <= days_left <= 2
                        and not clip.get("buster_payout_urgent_notified")):
                    with _LOCK:
                        clip["buster_payout_urgent_notified"] = True
                        _save_state()
                    _notify(f"⏳ БУСТЕР: на «{(clip.get('title') or '')[:60]}» осталось "
                            f"{days_left} дн окна выплаты ({b_cur}{payout}). Успей сдать: {b_form}")


def _check_health() -> None:
    st = _load_state()
    today = date.today().isoformat()
    with _LOCK:                    # снимки: план/клипы мутируют другие потоки
        accounts_snapshot = list(st["accounts"])
        plan_snapshot = list(st["plan"])
        clips_snapshot = dict(st["clips"])
    for acc in accounts_snapshot:
        posted = [p for p in plan_snapshot
                  if p["account_id"] == acc["id"] and p["status"] == "posted"]
        views = [clips_snapshot.get(p["clip_id"], {}).get("views") for p in posted]
        if account_health(views) == "warn":
            last = st["health_warned"].get(acc["id"], "")
            if last[:10] != today:  # max one warning per day per account
                with _LOCK:
                    st["health_warned"][acc["id"]] = today
                    _save_state()
                _notify(f"⚠️ Аккаунт «{acc['name']}»: последние ролики почти без просмотров. "
                        f"Похоже на теневой бан — проверь вручную и сделай паузу 2-3 дня.")


def _yt_ready(acc: Dict[str, Any]) -> bool:
    y = acc.get("yt") or {}
    return (acc.get("platform") == "youtube"
            and bool(y.get("client_id")) and bool(y.get("client_secret"))
            and bool(y.get("refresh_token")))


def _claim_plan(plan_id: str, expect: str, new: str) -> bool:
    """Атомарно сменить статус плана expect→new под _LOCK (compare-and-set).
    Возвращает True только если статус был РОВНО expect — тогда вызывающий
    «владеет» переходом. Главная защита от двойного автопоста: планировщик и
    кнопка «опубликовать сейчас» (или два прохода планировщика) не смогут оба
    схватить один клип и залить его на канал дважды."""
    with _LOCK:
        st = _load_state()
        p = next((x for x in st["plan"] if x["id"] == plan_id), None)
        if not p or p.get("status") != expect:
            return False
        p["status"] = new
        _save_state()
        return True


def _manual_post_notify(p: Dict[str, Any], clip: Dict[str, Any], acc: Dict[str, Any],
                        token: str, chat: str, prefix: str = "") -> None:
    """Ручной режим: бот шлёт файл + готовый текст для копипасты + кнопки."""
    if not (token and chat and clip.get("path") and Path(clip["path"]).exists()):
        return
    meta = _polish_meta(clip)
    cap = (f"{prefix}⏰ ПОРА ПОСТИТЬ\n"
           f"Аккаунт: {acc.get('name', '?')}\n\n"
           f"📌 Название:\n{meta['title']}\n\n"
           f"📝 Описание:\n{meta['description'][:600]}\n\n"
           f"Скачай видео и опубликуй с этим текстом.")
    kb = {"inline_keyboard": [[
        {"text": "🟢 Опубликовал", "callback_data": f"p:done:{p['id']}"},
        {"text": "⏭ Пропустить", "callback_data": f"p:skip:{p['id']}"},
    ]]}
    _send_video_retry(token, chat, Path(clip["path"]), cap, kb)


def _auto_publish(plan_id: str) -> None:
    """Загрузить клип на YouTube канала аккаунта (официальный API)."""
    st = _load_state()
    p = next((x for x in st["plan"] if x["id"] == plan_id), None)
    if not p or p["status"] != "publishing":
        return
    if p.get("video_id"):   # уже залит ранее → закрываем, не плодим дубль на канале
        _claim_plan(plan_id, "publishing", "posted")
        return
    clip = st["clips"].get(p["clip_id"]) or {}
    acc = next((a for a in st["accounts"] if a["id"] == p["account_id"]), {})
    token, chat = _tg_creds()
    # Аккаунт удалён/не подключён к YouTube между планированием и постингом — это
    # НЕ сетевой сбой, ретраить часами бессмысленно (раньше тут падал KeyError).
    if not _yt_ready(acc):
        with _LOCK:
            p["status"] = "notified"
            p.pop("attempts", None)
            p.pop("next_try", None)
            _save_state()
        _manual_post_notify(p, clip, acc, token, chat, prefix="⚠️ (нет подключения канала) ")
        return
    try:
        if not (clip.get("path") and Path(clip["path"]).exists()):
            raise RuntimeError("файл клипа не найден")
        meta = _polish_meta(clip)
        y = acc["yt"]
        vid = None
        last_err: Optional[Exception] = None
        # Сеть до Google нестабильна — до 3 попыток с паузой, потом фолбэк в бот.
        for attempt in range(1, 4):
            try:
                access = yt.refresh_access_token(y["client_id"], y["client_secret"],
                                                 y["refresh_token"])
                vid = yt.upload_video(access, clip["path"], meta["title"], meta["description"],
                                      tags=[t.lstrip("#") for t in meta["hashtags"]])
                break
            except yt.YouTubeAuthError:
                # Токен отозван/протух — ретрай не поможет, нужен реконнект канала.
                with _LOCK:
                    p["status"] = "notified"
                    p.pop("attempts", None)
                    p.pop("next_try", None)
                    _save_state()
                _notify(f"🔑 Канал «{acc.get('name', '?')}» нужно переподключить — "
                        f"токен отозван/протух. Открой дашборд и нажми «Подключить YouTube». "
                        f"Этот ролик пока кидаю ручным сообщением.")
                _manual_post_notify(p, clip, acc, token, chat, prefix="🔑 (переподключи канал) ")
                return
            except Exception as e:
                last_err = e
                if attempt < 3:
                    print(f"[clipper] автопост: попытка {attempt}/3 сорвалась "
                          f"({str(e)[:80]}), повтор через {20 * attempt}с...", flush=True)
                    time.sleep(20 * attempt)
        if vid is None:
            raise last_err or RuntimeError("upload failed")
        with _LOCK:
            # Перечитываем запись плана: её могли удалить (✕) или подменить во время
            # заливки. Тогда не теряем уже опубликованный ролик — video_id всё равно
            # оседает в clip, и _pull_yt_stats/выплаты его увидят.
            live = next((x for x in _load_state()["plan"] if x.get("id") == plan_id), None)
            if live is not None:
                live["status"] = "posted"
                live["video_id"] = vid
                live["marked_at"] = datetime.now().isoformat(timespec="seconds")
                live.setdefault("posted_at", live["marked_at"])   # неизменный якорь окна выплаты
                live.pop("attempts", None)
                live.pop("next_try", None)
            clip["yt_video_id"] = vid   # ВСЕГДА, даже если запись плана удалили во время заливки
            _save_state()
        _notify(f"✅ АВТОПОСТ: «{acc.get('name', '?')}»\n"
                f"«{(clip.get('title') or '')[:80]}»\nhttps://youtu.be/{vid}")
        print(f"[clipper] автопост ok: {acc.get('name')} -> {vid}", flush=True)
    except Exception as e:
        # Сеть владельца рвётся ВОЛНАМИ: вместо мгновенного фолбэка переносим
        # загрузку на позже (10, 20, 30... минут) — бот дожмёт сам. Ручное
        # сообщение шлём только когда исчерпали все попытки (~2.5 часа).
        attempts = int(p.get("attempts") or 0) + 1
        print(f"[clipper] автопост FAILED ({acc.get('name')}), серия {attempts}/"
              f"{MAX_PUBLISH_ATTEMPTS}: {e!r}", flush=True)
        if attempts < MAX_PUBLISH_ATTEMPTS:
            delay_min = 10 * attempts
            nt = datetime.fromtimestamp(time.time() + delay_min * 60)
            with _LOCK:
                p["status"] = "scheduled"
                p["attempts"] = attempts
                p["next_try"] = nt.isoformat(timespec="seconds")
                _save_state()
            if attempts == 1:   # одно уведомление, без спама на каждый повтор
                _notify(f"📶 Сеть до YouTube моргнула — автопост для "
                        f"«{acc.get('name', '?')}» повторю сам через {delay_min} мин "
                        f"(и ещё до {MAX_PUBLISH_ATTEMPTS - 1} попыток). Ничего делать не надо.")
        else:
            with _LOCK:
                p["status"] = "notified"   # все попытки исчерпаны → ручной режим
                p.pop("attempts", None)
                p.pop("next_try", None)
                _save_state()
            _notify(f"⚠️ Автопост для «{acc.get('name', '?')}» не пробился за "
                    f"{MAX_PUBLISH_ATTEMPTS} серий попыток: {str(e)[:120]}\n"
                    f"Кидаю ролик ручным сообщением.")
            _manual_post_notify(p, clip, acc, token, chat, prefix="⚠️ (автопост упал) ")


def _pull_yt_stats_once() -> int:
    """Один проход авто-статистики. Возвращает число обновлённых клипов."""
    st = _load_state()
    updated = 0
    with _LOCK:                    # снимки: план/аккаунты мутируют другие потоки
        accounts_snapshot = list(st["accounts"])
        plan_snapshot = list(st["plan"])
    for acc in accounts_snapshot:
        if not _yt_ready(acc):
            continue
        vids = {}
        for p in plan_snapshot:
            if p["account_id"] == acc["id"] and p.get("video_id"):
                clip = st["clips"].get(p["clip_id"])
                if clip is not None:
                    vids[p["video_id"]] = clip
        if not vids:
            continue
        try:
            y = acc["yt"]
            access = yt.refresh_access_token(y["client_id"], y["client_secret"],
                                             y["refresh_token"])
            stats = yt.fetch_stats(access, list(vids.keys()))
        except Exception as e:
            print(f"[clipper] stats pull failed ({acc.get('name')}): {str(e)[:120]}")
            continue
        with _LOCK:
            for vid, s in stats.items():
                clip = vids.get(vid)
                if clip is not None:
                    clip["views"] = s.get("views", clip.get("views"))
                    clip["likes"] = s.get("likes", clip.get("likes"))
                    updated += 1
            _save_state()
    _check_hot_clips()
    _check_health()
    return updated


def _stats_loop() -> None:
    """Каждые 4 часа сам тянет просмотры/лайки опубликованных YouTube-роликов."""
    time.sleep(90)   # дать серверу подняться
    while True:
        try:
            _pull_yt_stats_once()
        except Exception as e:
            print("[clipper] stats loop error:", repr(e))
        time.sleep(STATS_PULL_EVERY_S)


def _scheduler_loop() -> None:
    while True:
        try:
            token, chat = _tg_creds()
            st = _load_state()
            today = date.today().isoformat()
            now_hm = _now_hm()
            now_iso = datetime.now().isoformat(timespec="seconds")
            with _LOCK:
                plan_snapshot = list(st["plan"])   # снимок: план мутируют другие потоки
            for p in plan_snapshot:
                if p["status"] != "scheduled":
                    continue
                # Отложенный повтор после сетевого сбоя — ждём своего времени.
                if p.get("next_try") and p["next_try"] > now_iso:
                    continue
                if p["date"] < today or (p["date"] == today and p["slot"] <= now_hm):
                    clip = st["clips"].get(p["clip_id"]) or {}
                    acc = next((a for a in st["accounts"] if a["id"] == p["account_id"]), {})
                    if _yt_ready(acc):
                        # АВТОПОСТ: атомарно «забираем» клип (scheduled→publishing).
                        # Только если забрали именно мы — стартуем заливку, иначе
                        # его уже взял другой поток/кнопка (без публичного дубля).
                        if _claim_plan(p["id"], "scheduled", "publishing"):
                            threading.Thread(target=_auto_publish, args=(p["id"],),
                                             daemon=True, name=f"yt-pub-{p['id']}").start()
                    else:
                        if _claim_plan(p["id"], "scheduled", "notified"):
                            _manual_post_notify(p, clip, acc, token, chat)
            _check_hot_clips()
            _check_health()
        except Exception as e:
            print("[clipper] scheduler error:", repr(e))
        time.sleep(60)


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------
app = FastAPI(title="Нарезчик", version="1.0")


class EnqueueIn(BaseModel):
    category: str
    files: List[str]


class AccountIn(BaseModel):
    name: str
    category: str
    platform: str = ""            # "youtube" → авто-постинг; иначе ручной режим через бота
    yt_client_id: str = ""
    yt_client_secret: str = ""


class AccountDel(BaseModel):
    id: str


class StatsIn(BaseModel):
    clip_id: str
    views: int
    likes: Optional[int] = None


class MarkIn(BaseModel):
    plan_id: str
    status: str  # posted | skipped


@app.get("/")
def root():
    # no-cache: иначе браузер может держать старый HTML без новых кнопок
    return FileResponse(CLIPPER_DIR / "dashboard.html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health():
    return {"ok": True, "tool": "clipper"}


BUSTER_CHECKLIST = [
    "Buster — главное лицо в клипе (иначе заявку отклонят)",
    "Реклама/баннеры на видео замазаны",
    "Ролик опубликован не больше 14 дней назад — успей сдать в окно",
    "Момент из стрима после 01.01.2026 (старое не принимают)",
    "Без накрутки просмотров/лайков/комментариев",
    "Без чужого/перезалитого видео",
    "Без фейковых реакций",
    "#buster и twitch.tv/buster в заголовке и описании (вшиваются автоматически)",
]


def _buster_state(st: Dict[str, Any]) -> Dict[str, Any]:
    """Сводка Buster-вертикали для дашборда: заработок $, право на выплату, чек-лист."""
    s = _settings()
    table = s.get("buster_payout_table") or []
    window = int(s.get("buster_payout_window_days", 14) or 14)
    cur = s.get("buster_payout_currency") or "$"
    now = datetime.now()
    earn = buster_earnings(st["clips"], st["plan"], st["accounts"], table, BUSTER_CAT)
    b_accs = [a for a in st["accounts"] if a.get("category") == BUSTER_CAT]
    accounts = [{"id": a["id"], "name": a.get("name"), "yt_connected": _yt_ready(a),
                 "earned": earn["by_account"].get(a["id"], 0)} for a in b_accs]
    acc_name = {a["id"]: a.get("name") for a in b_accs}
    clips = []
    for r in earn["rows"]:
        clip = st["clips"].get(r["clip_id"]) or {}
        days_left = days_left_to_payout(r["posted_at"], window, now)
        clips.append({
            "clip_id": r["clip_id"],
            "title": clip.get("title") or r["clip_id"],
            "account": acc_name.get(r["account_id"], "?"),
            "views": r["views"],
            "payout": r["payout"],
            "posted_at": r["posted_at"],
            "days_left": days_left,
            "payout_due": (r["payout"] > 0) and (days_left is None or days_left >= 0),
            "submitted": bool(clip.get("buster_submitted")),
        })
    # вперёд — то, что можно/пора сдавать (payout>0, окно открыто), по убыванию срочности
    clips.sort(key=lambda c: (c["submitted"], not c["payout_due"],
                              c["days_left"] if isinstance(c["days_left"], int) else 999))
    return {
        "enabled": bool(s.get("buster_enabled", True)),
        "total_earned": earn["total"],
        "currency": cur,
        "accounts": accounts,
        "accounts_count": len(b_accs),
        "max_accounts": int(s.get("buster_max_accounts", 10) or 10),
        "clips": clips,
        "form_url": s.get("buster_payout_form_url") or "",
        "contact": s.get("buster_payout_contact") or "",
        "window_days": window,
        "table": table,
        "streamer": s.get("buster_streamer") or "buster",
        "channel_url": s.get("buster_channel_url") or "twitch.tv/buster",
        "checklist": BUSTER_CHECKLIST,
    }


@app.get("/state")
def get_state():
    st = _load_state()
    clips = sorted(st["clips"].values(), key=lambda c: c.get("created") or "", reverse=True)
    accounts = []
    for a in st["accounts"]:
        posted = [p for p in st["plan"] if p["account_id"] == a["id"] and p["status"] == "posted"]
        planned = [p for p in st["plan"] if p["account_id"] == a["id"] and p["status"] in ("scheduled", "notified")]
        views = [st["clips"].get(p["clip_id"], {}).get("views") for p in posted]
        total = sum(v for v in views if isinstance(v, int))
        pub = {k: v for k, v in a.items() if k != "yt"}   # не светим секреты в UI
        pub["yt_connected"] = _yt_ready(a)
        pub["auto"] = _yt_ready(a)
        pub["warmed"] = a.get("warmed", True)   # старые аккаунты считаем прогретыми
        accounts.append({**pub, "posted": len(posted), "planned": len(planned),
                         "total_views": total, "health": account_health(views)})
    return {
        "categories": [{"key": k, "label": l, "folder": str(SOURCES_DIR / CAT_FOLDER[k])}
                       for k, l in CATEGORIES],
        "sources": _list_sources(),
        "queue": st["queue"][-50:],
        "clips": clips[:200],
        "accounts": accounts,
        # Активные посты показываем ВСЕ (иначе при разросшейся истории будущие
        # строки выпадут из среза), завершённые — последние 150.
        "plan": (lambda sp: [p for p in sp if p["status"] in ("scheduled", "notified", "publishing")]
                 + [p for p in sp if p["status"] not in ("scheduled", "notified", "publishing")][-150:])(
                     sorted(st["plan"], key=lambda p: (p["date"], p["slot"]))),
        "tg_connected": all(_tg_creds()),
        "clips_per_10min": int(st.get("clips_per_10min") or _settings().get("clips_per_10min") or 3),
        # Buster никогда не должен ронять /state (дашборд опрашивает каждые 3с).
        "buster": _safe_buster_state(st),
    }


def _safe_buster_state(st: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return _buster_state(st)
    except Exception as e:
        print("[clipper] buster state error:", str(e)[:140])
        return {"enabled": False}


@app.post("/enqueue")
def enqueue(body: EnqueueIn):
    if body.category not in CAT_FOLDER:
        return JSONResponse({"error": "unknown category"}, status_code=400)
    st = _load_state()
    added = 0
    with _LOCK:
        queued = {(q["category"], q["file"]) for q in st["queue"]
                  if q["status"] in ("pending", "processing") and q.get("file")}
        for f in body.files:
            if (body.category, f) in queued:
                continue
            if not (SOURCES_DIR / CAT_FOLDER[body.category] / f).exists():
                continue
            st["queue"].append({"id": uuid.uuid4().hex[:8], "file": f,
                                "category": body.category, "status": "pending",
                                "added": datetime.now().isoformat(timespec="seconds")})
            added += 1
        _save_state()
    return {"queued": added}


class ConfigIn(BaseModel):
    clips_per_10min: int = 3


@app.post("/config")
def set_config(body: ConfigIn):
    """Владелец выбирает, сколько клипов резать на 10 минут источника (1..20)."""
    n = max(1, min(int(body.clips_per_10min or 3), 20))
    with _LOCK:
        _load_state()["clips_per_10min"] = n
        _save_state()
    return {"ok": True, "clips_per_10min": n}


@app.post("/accounts/add")
def accounts_add(body: AccountIn):
    if body.category not in CAT_FOLDER or not body.name.strip():
        return JSONResponse({"error": "bad account"}, status_code=400)
    platform = body.platform.strip().lower()
    if platform == "youtube" and not (body.yt_client_id.strip() and body.yt_client_secret.strip()):
        return JSONResponse({"error": "для YouTube нужны client_id и client_secret "
                                      "(Google Cloud Console → Credentials → OAuth Desktop app)"},
                            status_code=400)
    st = _load_state()
    with _LOCK:
        # Buster: только YouTube и жёсткий лимит аккаунтов (правила программы).
        if body.category == BUSTER_CAT:
            s = _settings()
            if platform != "youtube":
                return JSONResponse({"error": "Бустер: аккаунт должен быть YouTube — "
                                              "вертикаль работает только с YouTube Shorts"},
                                    status_code=400)
            limit = int(s.get("buster_max_accounts", 10) or 10)
            if sum(1 for a in st["accounts"] if a.get("category") == BUSTER_CAT) >= limit:
                return JSONResponse({"error": f"Бустер: лимит {limit} аккаунта по правилам "
                                              f"программы — больше добавить нельзя"},
                                    status_code=400)
        acc: Dict[str, Any] = {"id": uuid.uuid4().hex[:8], "name": body.name.strip(),
                               "category": body.category, "platform": platform,
                               # Мягкий режим: не блокируем (ферма каналов на продажу),
                               # 🧊 — ручной тормоз на время отлёжки, если нужно.
                               "warmed": True}
        if platform == "youtube":
            acc["yt"] = {"client_id": body.yt_client_id.strip(),
                         "client_secret": body.yt_client_secret.strip(),
                         "refresh_token": ""}
        st["accounts"].append(acc)
        _save_state()
    return {"ok": True, "id": acc["id"]}


@app.post("/accounts/del")
def accounts_del(body: AccountDel):
    st = _load_state()
    with _LOCK:
        st["accounts"] = [a for a in st["accounts"] if a["id"] != body.id]
        st["plan"] = [p for p in st["plan"]
                      if p["account_id"] != body.id or p["status"] in ("posted", "skipped")]
        _save_state()
    return {"ok": True}


class AccountPayoutIn(BaseModel):
    id: str
    wallet: str = ""
    login: str = ""


@app.post("/accounts/payout")
def accounts_payout(body: AccountPayoutIn):
    """USDT-кошелёк (TRC-20) и логин канала для автопакета выплаты buster."""
    with _LOCK:
        st = _load_state()
        acc = next((a for a in st["accounts"] if a["id"] == body.id), None)
        if not acc:
            return JSONResponse({"error": "аккаунт не найден"}, status_code=404)
        acc["payout_wallet"] = body.wallet.strip()
        acc["login"] = body.login.strip()
        _save_state()
    return {"ok": True}


def _build_plan() -> tuple:
    """Собрать расписание для всех одобренных, ещё не запланированных клипов.
    Возвращает (добавлено, пропущено-непрогретых). Дёргается и кнопкой «Сформировать
    план», и АВТОМАТИЧЕСКИ после одобрения клипа в Telegram."""
    st = _load_state()
    with _LOCK:
        planned_clips = {p["clip_id"] for p in st["plan"]}
        built = 0
        unwarmed = 0
        for cat_key, _ in CATEGORIES:
            # Стратегия прогрева: на непрогретые аккаунты посты не планируем.
            unwarmed += sum(1 for a in st["accounts"]
                            if a["category"] == cat_key and not a.get("warmed", True))
            accs = [a["id"] for a in st["accounts"]
                    if a["category"] == cat_key and a.get("warmed", True)]
            clips = [c["id"] for c in sorted(st["clips"].values(),
                                             key=lambda c: -(c.get("score") or 0))
                     if c["category"] == cat_key and c["status"] == "approved"
                     and c["id"] not in planned_clips]
            if not accs or not clips:
                continue
            backlog = {a: sum(1 for p in st["plan"] if p["account_id"] == a
                              and p["status"] in ("scheduled", "notified")) for a in accs}
            # busy = реально ЗАНЯТЫЕ слоты на (аккаунт, дата) — любая существующая
            # запись плана бронирует свой слот, чтобы build_schedule не положил в
            # него второй ролик (раньше был счётчик → коллизии слотов).
            busy: Dict[str, Dict[str, set]] = {}
            for p in st["plan"]:
                if p.get("slot"):
                    busy.setdefault(p["account_id"], {}).setdefault(p["date"], set()).add(p["slot"])
            assignments = distribute(clips, accs, backlog)
            entries = build_schedule(assignments, date.today(), busy, now_hm=_now_hm())
            for e in entries:
                e["id"] = uuid.uuid4().hex[:8]
                e["status"] = "scheduled"
                st["plan"].append(e)
                built += 1
        _save_state()
    return built, unwarmed


@app.post("/plan/build")
def plan_build():
    built, unwarmed = _build_plan()
    return {"planned": built, "unwarmed": unwarmed}


@app.post("/stats/set")
def stats_set(body: StatsIn):
    st = _load_state()
    clip = st["clips"].get(body.clip_id)
    if not clip:
        return JSONResponse({"error": "no such clip"}, status_code=404)
    with _LOCK:
        clip["views"] = int(body.views)
        if body.likes is not None:
            clip["likes"] = int(body.likes)
        _save_state()
    _check_hot_clips()
    return {"ok": True}


@app.post("/post/mark")
def post_mark(body: MarkIn):
    st = _load_state()
    entry = next((p for p in st["plan"] if p["id"] == body.plan_id), None)
    if not entry or body.status not in ("posted", "skipped"):
        return JSONResponse({"error": "bad request"}, status_code=400)
    with _LOCK:
        entry["status"] = body.status
        entry["marked_at"] = datetime.now().isoformat(timespec="seconds")
        if body.status == "posted":
            entry.setdefault("posted_at", entry["marked_at"])  # якорь окна выплаты
        _save_state()
    return {"ok": True}


class BusterSubmitIn(BaseModel):
    clip_id: str
    submitted: bool = True


@app.post("/buster/submit")
def buster_submit(body: BusterSubmitIn):
    """Отметить buster-клип как сданный на выплату (форму заполняет владелец сам).

    Это просто бухгалтерия дашборда: сданные перестают маячить и не шлют повторных
    напоминаний. Ничего никуда не отправляет.
    """
    st = _load_state()
    clip = st["clips"].get(body.clip_id)
    if not clip:
        return JSONResponse({"error": "no such clip"}, status_code=404)
    with _LOCK:
        if body.submitted:
            clip["buster_submitted"] = True
            clip["buster_submitted_at"] = datetime.now().isoformat(timespec="seconds")
        else:
            clip.pop("buster_submitted", None)
            clip.pop("buster_submitted_at", None)
        _save_state()
    return {"ok": True}


class DownloadIn(BaseModel):
    url: str
    category: str
    music: bool = False        # галочка «🎵 с музыкой»: фон у клипов ЭТОЙ закачки


@app.post("/download")
def download(body: DownloadIn):
    """Ссылка на YouTube или RuTube → очередь: скачать (лучшее ≤1080p) и нарезать."""
    from clipper.downloader import looks_like_supported
    url = body.url.strip()
    if body.category not in CAT_FOLDER:
        return JSONResponse({"error": "unknown category"}, status_code=400)
    if not looks_like_supported(url):
        return JSONResponse({"error": "ссылка не распознана — нужна YouTube / RuTube / VK Видео / OK.ru"}, status_code=400)
    st = _load_state()
    with _LOCK:
        if any(q.get("url") == url and q["status"] in ("pending", "processing")
               for q in st["queue"]):
            return JSONResponse({"error": "эта ссылка уже в очереди"}, status_code=400)
        st["queue"].append({"id": uuid.uuid4().hex[:8], "file": "⬇ " + url[:60],
                            "url": url, "category": body.category, "status": "pending",
                            "music": bool(body.music),
                            "added": datetime.now().isoformat(timespec="seconds")})
        _save_state()
    return {"ok": True}


class MusicDelIn(BaseModel):
    category: str = "common"
    name: str


@app.get("/music/list")
def music_list():
    """Список загруженных royalty-free треков по категориям."""
    out = {}
    for cat in MUSIC_CATS:
        d = MUSIC_DIR if cat == "common" else MUSIC_DIR / cat
        files = []
        if d.is_dir():
            files = sorted(p.name for p in d.iterdir()
                           if p.is_file() and p.suffix.lower() in MUSIC_EXTS)
        out[cat] = files
    vol = float(_load_state().get("bg_music_volume", 0.12) or 0.12)
    return {"music": out, "volume": vol}


@app.post("/music/upload")
async def music_upload(file: UploadFile = File(...), category: str = Form("common")):
    """Загрузить аудио-трек в assets/music/<категория>/ — фон для клипов."""
    cat = (category or "common").strip()
    if cat not in MUSIC_CATS:
        return JSONResponse({"error": "неизвестная категория"}, status_code=400)
    raw = os.path.basename((file.filename or "").strip())
    ext = os.path.splitext(raw)[1].lower()
    if ext not in MUSIC_EXTS:
        return JSONResponse({"error": "только аудио: mp3/m4a/wav/ogg/opus/aac"}, status_code=400)
    data = await file.read()
    if not data:
        return JSONResponse({"error": "пустой файл"}, status_code=400)
    if len(data) > 30 * 1024 * 1024:
        return JSONResponse({"error": "трек больше 30 МБ — возьми покороче"}, status_code=400)
    dest_dir = MUSIC_DIR if cat == "common" else MUSIC_DIR / cat
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\-]+", "_", raw)[:80] or ("track" + ext)
    if not safe.lower().endswith(ext):
        safe += ext
    (dest_dir / safe).write_bytes(data)
    return {"ok": True, "saved": safe, "category": cat}


@app.post("/music/delete")
def music_delete(body: MusicDelIn):
    """Удалить загруженный трек."""
    cat = (body.category or "common").strip()
    if cat not in MUSIC_CATS:
        return JSONResponse({"error": "неизвестная категория"}, status_code=400)
    name = os.path.basename(body.name or "")
    f = (MUSIC_DIR if cat == "common" else MUSIC_DIR / cat) / name
    try:
        if f.is_file():
            f.unlink()
    except Exception as e:
        return JSONResponse({"error": str(e)[:120]}, status_code=400)
    return {"ok": True}


class MusicVolIn(BaseModel):
    volume: float


@app.post("/music/volume")
def music_volume(body: MusicVolIn):
    """Громкость фоновой музыки (0..1, дефолт 0.12). Хранится в state."""
    v = max(0.0, min(1.0, float(body.volume)))
    with _LOCK:
        _load_state()["bg_music_volume"] = v
        _save_state()
    return {"ok": True, "volume": v}


class WarmIn(BaseModel):
    id: str
    warmed: bool


@app.post("/accounts/warm")
def accounts_warm(body: WarmIn):
    """Отметить аккаунт прогретым (стратегия: отлёжка → активность → шортсы)."""
    st = _load_state()
    acc = next((a for a in st["accounts"] if a["id"] == body.id), None)
    if not acc:
        return JSONResponse({"error": "no such account"}, status_code=404)
    with _LOCK:
        acc["warmed"] = bool(body.warmed)
        _save_state()
    return {"ok": True}


class RetryIn(BaseModel):
    id: str


@app.post("/queue/retry")
def queue_retry(body: RetryIn):
    """Упавшую задачу нарезки — обратно в очередь."""
    st = _load_state()
    q = next((x for x in st["queue"] if x["id"] == body.id), None)
    if not q or q["status"] != "failed":
        return JSONResponse({"error": "задача не найдена или не в статусе failed"}, status_code=400)
    with _LOCK:
        q["status"] = "pending"
        q.pop("error", None)
        _save_state()
    return {"ok": True}


@app.post("/queue/cancel")
def queue_cancel(body: RetryIn):
    """Отменить задачу: pending/failed/done — убрать из очереди; processing —
    пометить на отмену (воркер остановит скачивание/нарезку и приберёт исходник)."""
    with _LOCK:        # проверка статуса и ветка — под одним локом, согласованно с воркером
        st = _load_state()
        q = next((x for x in st["queue"] if x["id"] == body.id), None)
        if not q:
            return {"ok": True}
        if q["status"] == "processing":
            _CANCEL.add(body.id)
            return {"ok": True, "state": "останавливаю"}
        st["queue"] = [x for x in st["queue"] if x["id"] != body.id]
        _save_state()
    return {"ok": True, "state": "убрано"}


@app.post("/plan/remove")
def plan_remove(body: RetryIn):
    """Удалить ОДНУ запись плана. Кроме статуса 'publishing' — идёт заливка, удаление
    осиротило бы уже публикуемый ролик (потеря video_id/статистики/выплаты)."""
    with _LOCK:
        st = _load_state()
        tgt = next((p for p in st["plan"] if p.get("id") == body.id), None)
        if tgt and tgt.get("status") == "publishing":
            return {"ok": False, "error": "идёт публикация — нельзя удалить, подожди пару минут"}
        before = len(st["plan"])
        st["plan"] = [p for p in st["plan"] if p.get("id") != body.id]
        _save_state()
    return {"ok": True, "removed": before - len(st["plan"])}


class PublishIn(BaseModel):
    plan_id: str


@app.post("/post/publish")
def post_publish(body: PublishIn):
    """🚀 Опубликовать прямо сейчас (не дожидаясь слота). Только YouTube-авто."""
    st = _load_state()
    entry = next((p for p in st["plan"] if p["id"] == body.plan_id), None)
    if not entry or entry["status"] not in ("scheduled", "notified"):
        return JSONResponse({"error": "пост не найден или уже обработан"}, status_code=400)
    acc = next((a for a in st["accounts"] if a["id"] == entry["account_id"]), None)
    if not acc or not _yt_ready(acc):
        return JSONResponse({"error": "аккаунт не подключен к YouTube — опубликуй вручную "
                                      "и нажми «🟢 опубликовал»"}, status_code=400)
    # Атомарно забираем пост (scheduled/notified → publishing). Если не вышло —
    # его уже схватил планировщик или другое нажатие, второй заливки не будет.
    if not (_claim_plan(entry["id"], "scheduled", "publishing")
            or _claim_plan(entry["id"], "notified", "publishing")):
        return JSONResponse({"error": "пост уже публикуется или обработан"}, status_code=400)
    threading.Thread(target=_auto_publish, args=(entry["id"],),
                     daemon=True, name=f"yt-pubnow-{entry['id']}").start()
    return {"ok": True}


@app.get("/auth/yt/start")
def yt_auth_start(id: str):
    """Открывает Google-консент для канала аккаунта id (loopback OAuth)."""
    st = _load_state()
    acc = next((a for a in st["accounts"] if a["id"] == id), None)
    if not acc or acc.get("platform") != "youtube" or not acc.get("yt", {}).get("client_id"):
        return JSONResponse({"error": "аккаунт не найден или не YouTube"}, status_code=400)
    url = yt.build_auth_url(acc["yt"]["client_id"], _yt_redirect(), state=id)
    return RedirectResponse(url)


@app.get("/auth/yt/callback")
def yt_auth_callback(state: str = "", code: str = "", error: str = ""):
    if error or not (state and code):
        return HTMLResponse(f"<h3>❌ Не подключено: {error or 'нет кода'}</h3>", status_code=400)
    st = _load_state()
    acc = next((a for a in st["accounts"] if a["id"] == state), None)
    if not acc or not acc.get("yt"):
        return HTMLResponse("<h3>❌ Аккаунт не найден</h3>", status_code=404)
    try:
        tokens = yt.exchange_code(acc["yt"]["client_id"], acc["yt"]["client_secret"],
                                  code, _yt_redirect())
    except Exception as e:
        return HTMLResponse(f"<h3>❌ Ошибка обмена кода: {str(e)[:300]}</h3>", status_code=500)
    with _LOCK:
        acc["yt"]["refresh_token"] = tokens["refresh_token"]
        _save_state()
    _notify(f"🔗 YouTube подключен: «{acc['name']}» — автопостинг активен.")
    return HTMLResponse(
        "<div style='font:16px/1.6 Segoe UI;max-width:480px;margin:80px auto;text-align:center'>"
        f"<h2>✅ Канал «{acc['name']}» подключен</h2>"
        "<p>Автопубликация включена. Эту вкладку можно закрыть.</p></div>")


class YtManualIn(BaseModel):
    id: str
    paste: str


@app.post("/auth/yt/manual")
def yt_auth_manual(body: YtManualIn):
    """Ручное подключение YouTube для серверного деплоя: Google редиректит на
    localhost (недоступен с сервера), поэтому владелец копирует адрес из строки
    браузера и вставляет сюда — достаём ?code= и меняем на refresh_token."""
    st = _load_state()
    acc = next((a for a in st["accounts"] if a["id"] == body.id), None)
    if not acc or not acc.get("yt"):
        return JSONResponse({"error": "аккаунт не найден"}, status_code=404)
    raw = body.paste or ""
    m = re.search(r"[?&]code=([^&\s]+)", raw)
    code = urllib.parse.unquote(m.group(1) if m else raw.strip())
    if not code:
        return JSONResponse({"error": "не нашёл код — вставь адрес целиком"}, status_code=400)
    try:
        tokens = yt.exchange_code(acc["yt"]["client_id"], acc["yt"]["client_secret"],
                                  code, _yt_redirect())
    except Exception as e:
        return JSONResponse({"error": f"обмен кода не удался: {str(e)[:200]}"}, status_code=500)
    with _LOCK:
        acc["yt"]["refresh_token"] = tokens["refresh_token"]
        _save_state()
    _notify(f"🔗 YouTube подключен: «{acc['name']}» — автопостинг активен.")
    return {"ok": True}


@app.post("/tg/test")
def tg_test():
    token, chat = _tg_creds()
    if not (token and chat):
        return JSONResponse({"error": "нет telegram_bot_token/telegram_chat_id в настройках завода"},
                            status_code=400)
    _notify("🔪 Нарезчик подключен и на связи!")
    return {"ok": True}


@app.on_event("startup")
def _startup():
    _ensure_dirs()
    # Необязательный исходящий прокси (ключ "https_proxy" в data/settings.json):
    # urllib/yt-dlp читают эти переменные — лечит вечные SSL-таймауты до
    # Google/Telegram на нестабильной сети. Это про связность, не про обходы.
    proxy = (_settings().get("https_proxy") or "").strip()
    if proxy:
        os.environ["HTTPS_PROXY"] = proxy
        os.environ["HTTP_PROXY"] = proxy
        print(f"[clipper] исходящий прокси включён ({proxy.split('@')[-1][:40]})", flush=True)
    st = _load_state()
    with _LOCK:
        # Если сервер упал посреди загрузки — вернуть пост в очередь. Старые
        # версии могли оставить и 'failed' в плане — это тупик без кнопок,
        # мигрируем обратно в расписание.
        for p in st["plan"]:
            # 'failed' — ролик НЕ постился, можно спокойно вернуть в расписание.
            if p.get("status") == "failed":
                p["status"] = "scheduled"
            # 'publishing' — аплоад был В ПРОЦЕССЕ при падении и МОГ уже залиться.
            # Авто-повтор создал бы ДУБЛЬ публичного ролика, поэтому уводим в
            # ручной режим: владелец сам решит (запостить/пропустить) из дашборда.
            elif p.get("status") == "publishing":
                p["status"] = "notified"
                p.pop("attempts", None)
                p.pop("next_try", None)
            # Якорь 14-дневного окна выплаты для уже опубликованных (старые записи).
            if p.get("status") == "posted" and not p.get("posted_at"):
                p["posted_at"] = p.get("marked_at") or datetime.now().isoformat(timespec="seconds")
        # ОЧЕРЕДЬ: задача в 'processing' при старте = воркер был убит рестартом/крашем
        # посреди скачивания/нарезки. Воркер берёт только 'pending', поэтому без этого
        # она осиротеет НАВСЕГДА (ровно этот баг и случился). Возвращаем в 'pending':
        # если не докачали — перекачать с нуля; если уже скачано — перережется.
        for q in st["queue"]:
            if q.get("status") == "processing":
                q["status"] = "pending"
                q.pop("error", None)
                # «file» НЕ трогаем — иначе падает q["file"] в списке источников/очереди.
                # Перекачку обеспечивает downloaded: не докачано → воркер скачает заново.
                print(f"[clipper] осиротевшая задача возвращена в очередь: "
                      f"{str(q.get('url') or q.get('file') or q.get('id'))[:60]}", flush=True)
        _save_state()
    threading.Thread(target=_worker_loop, daemon=True, name="clipper-worker").start()
    threading.Thread(target=_tg_poller_loop, daemon=True, name="clipper-tg").start()
    threading.Thread(target=_scheduler_loop, daemon=True, name="clipper-sched").start()
    threading.Thread(target=_stats_loop, daemon=True, name="clipper-stats").start()
    print("[clipper] Нарезчик запущен: http://127.0.0.1:8002", flush=True)
