# -*- coding: utf-8 -*-
"""ClipAgent v2 — finds the most "viral" moments in a long video's transcript.

The brain of render_mode="clip". Built after studying how OpusClip/Klap rank
clips (Hook / Flow / Value / Trend + the 3-second retention rule):

  STAGE A (LLM, chunked scan)
      The FULL transcript is scanned in chunks (not downsampled), so a 30-60 min
      video is fully covered. Each chunk call returns CANDIDATE moments with a
      per-dimension virality rubric:
        hook 0-30      — does the opening line stop the scroll?
        flow 0-25      — self-contained thought with a satisfying end?
        value 0-20     — insight / benefit / "aha"?
        emotion 0-15   — laughter, surprise, conflict, passion?
        quote 0-10     — memorable, shareable phrasing?
      plus the EXACT hook phrase from the transcript.

  STAGE B (deterministic, no LLM — reliability)
      - snap the start to the sentence that contains the hook (clip OPENS on the
        hook, never mid-sentence);
      - snap the end to a sentence end (no mid-word cut-offs);
      - drop overlaps/near-duplicates (greedy by score);
      - keep top-N, returned in rank order (best first → clip_01 = best).

  _local() fallback keeps the old even-spaced logic (score 50) so the pipeline
  NEVER breaks without a key.

Return shape: {"moments": [{"start","end","title","caption","reason","score","hook"}]}
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .base import AgentContext, BaseAgent

MIN_CLIP_S = 15.0          # жёсткий минимум (решение владельца: 15–60с)
SWEET_MIN_S = 15.0         # предпочтительное окно = весь допустимый диапазон:
SWEET_MAX_S = 60.0         # длина вторична, ГЛАВНОЕ — законченная мысль
MAX_CLIP_S = 60.0          # жёсткий потолок (Shorts)
DEFAULT_CLIP_S = 32.0

# Stage A chunking: full coverage of long videos without blowing the context.
# Sized for Groq free tier (12k tokens/min): ~9k chars ≈ 4-5k tokens per call.
CHUNK_CHAR_BUDGET = 9000
MAX_CHUNKS = 10
CANDIDATES_PER_CHUNK = 10
CHUNK_RETRY_SLEEP_S = 25    # one retry per chunk after a 429 (TPM window refill)
CHUNK_SPACING_S = 4.0       # pause between chunk calls (keeps TPM under the cap)

RUBRIC_MAX = {"hook": 30, "flow": 25, "value": 20, "emotion": 15, "quote": 10}

# Типы хуков (банк из контент-завода) — модель классифицирует заход каждого
# кандидата, а финальный отбор тянет РАЗНЫЕ типы (матрица углов: 3 клипа не на
# одно лицо). Значения — то, что модель должна вернуть в поле hook_type.
HOOK_TYPES = ["вопрос", "цифра", "провокация", "личный_опыт", "контраст", "петля"]

# Плотность речи (этап B, по пословным таймкодам): клип с «мёртвым воздухом»
# внутри проседает в удержании. Считаем самую длинную паузу и темп речи.
DEAD_AIR_S = 2.5            # пауза длиннее этого внутри клипа = штраф
DEAD_AIR_PENALTY_MAX = 18  # потолок штрафа за тишину (в баллах рубрики 0-100)
SPARSE_WPS = 1.2           # слов/сек ниже этого = вяло, лёгкий штраф


def _fmt_ts(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _norm_words(s: str) -> List[str]:
    return re.sub(r"[^\w\s]", " ", (s or "").lower()).split()


def _contains_sub(haystack: List[str], needle: List[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    n = len(needle)
    for i in range(len(haystack) - n + 1):
        if haystack[i:i + n] == needle:
            return True
    return False


def _build_sentences(segments: List[Dict]) -> List[Dict]:
    """Group whisper segments into sentence-ish units (for clean cut points)."""
    sents: List[Dict] = []
    cur: Optional[Dict] = None
    for s in segments:
        try:
            a, b = float(s["start"]), float(s["end"])
        except Exception:
            continue
        t = (s.get("text") or "").strip()
        if not t:
            continue
        if cur is None:
            cur = {"start": a, "end": b, "text": t}
        else:
            cur["end"] = b
            cur["text"] += " " + t
        # Sentence boundary: closing punctuation, or a runaway-length guard.
        if t[-1] in ".!?…" or (cur["end"] - cur["start"]) >= 22.0:
            sents.append(cur)
            cur = None
    if cur:
        sents.append(cur)
    return sents


class ClipAgent(BaseAgent):
    name = "clip_agent"

    # ------------------------------------------------------------- STAGE A+B
    def _llm(self, ctx: AgentContext, **extra: Any) -> Dict[str, Any]:
        transcript: Dict[str, Any] = extra.get("transcript") or {}
        segments: List[Dict] = transcript.get("segments") or []
        target_count: int = int(extra.get("target_count") or 3)
        duration: Optional[float] = extra.get("source_duration")

        if not segments:
            raise ValueError("no transcript segments for LLM moment selection")
        if not duration:
            try:
                duration = float(segments[-1]["end"])
            except Exception:
                duration = None

        words: List[Dict] = transcript.get("words") or []
        candidates = self._scan_candidates(ctx, segments, target_count)
        if not candidates:
            raise ValueError("LLM scan produced no candidates")

        sentences = _build_sentences(segments)
        # Keep a wider pool than needed — the judge re-ranks it cross-chunk and the
        # diversity pass then picks varied angles from the top.
        pool = self._refine_and_rank(candidates, sentences, duration,
                                     keep_n=min(target_count * 2 + 6, 20), words=words)
        if not pool:
            raise ValueError("no candidates survived boundary refinement")

        ranked = self._judge(ctx, pool, target_count, sentences)

        # Quality floor: drop weak picks (<55) as long as 2 strong ones remain.
        strong = [m for m in ranked if (m.get("score") or 0) >= 55]
        if len(strong) >= 2:
            ranked = strong
        if not ranked:
            raise ValueError("judge rejected all candidates")

        # C: финал — РАЗНЫЕ по типу захода моменты (матрица углов), clip_01 = топ.
        moments = self._select_diverse(ranked, target_count)
        # Гарантия количества: на длинном видео LLM часто упирается в квоту Groq и
        # отдаёт мало клипов — добиваем равномерными до нормы (владельцу важно ЧИСЛО).
        if len(moments) < target_count:
            moments = self._fill_to_count(moments, sentences, duration, target_count)
        return {"moments": moments[:target_count]}

    # ---------------------------------------------------------------- Stage A
    def _scan_candidates(self, ctx: AgentContext, segments: List[Dict],
                         target_count: int) -> List[Dict]:
        """Chunked LLM scan over the FULL transcript → scored candidates."""
        from . import llm_client

        # Build "[123s 02:03] text" lines, then split into char-budget chunks.
        lines: List[Tuple[float, str]] = []
        for s in segments:
            try:
                t = float(s["start"])
            except Exception:
                continue
            txt = (s.get("text") or "").strip().replace("\n", " ")
            if txt:
                lines.append((t, f"[{int(t)}s {_fmt_ts(t)}] {txt[:160]}"))

        chunks: List[str] = []
        buf: List[str] = []
        size = 0
        for _, ln in lines:
            if size + len(ln) > CHUNK_CHAR_BUDGET and buf:
                chunks.append("\n".join(buf))
                buf, size = [], 0
            buf.append(ln)
            size += len(ln) + 1
        if buf:
            chunks.append("\n".join(buf))
        if len(chunks) > MAX_CHUNKS:
            # Очень длинное видео: НЕ выбрасываем куски (так терялись виральные
            # моменты из пропущенных кусков — а доктрина модуля обещает полное
            # покрытие), а СЛИВАЕМ соседние в ≤MAX_CHUNKS более крупных. Весь
            # транскрипт остаётся просканированным, просто кусками побольше.
            group = (len(chunks) + MAX_CHUNKS - 1) // MAX_CHUNKS    # ceil без import
            chunks = ["\n".join(chunks[i:i + group]) for i in range(0, len(chunks), group)]

        system = (
            "Ты — лучший редактор вирусных коротких видео (TikTok/Reels/Shorts). "
            "Тебе дают кусок транскрипта длинного видео с таймкодами. Найди в нём "
            "САМОСТОЯТЕЛЬНЫЕ моменты-кандидаты на вирусный клип от 10 до 60 секунд. "
            "Длина вторична — ГЛАВНОЕ, чтобы момент открывался сильным хуком (вопрос, "
            "дерзкое заявление, интрига, неожиданный поворот, начало истории) и "
            "заканчивался ПОЛНОСТЬЮ завершённой мыслью: никогда не обрывай историю, "
            "шутку или объяснение на середине — лучше длиннее, но цельно. "
            "Оцени КАЖДЫЙ кандидат по рубрике: hook 0-30 (цепляет ли первая фраза), "
            "flow 0-25 (цельность и логичное завершение), value 0-20 (польза/инсайт), "
            "emotion 0-15 (эмоция: смех, шок, спор, вдохновение), quote 0-10 (цитатность). "
            "Будь строгим: средний момент = 40-55 суммарно, сильный = 70+, не завышай. "
            "Определи ТИП захода (hook_type) — один из: "
            "вопрос (открывается вопросом к зрителю), "
            "цифра (шокирующая цифра/статистика), "
            "провокация (дерзкое/спорное заявление), "
            "личный_опыт (история «я сделал…»), "
            "контраст (было→стало, до/после), "
            "петля (открытая интрига: обещание, ответ в конце). "
            "ПРОХОДНЫЕ куски (бытовая болтовня, переходы, «едем/обедаем», реклама канала) "
            "НЕ предлагай вообще — лучше вернуть 3 сильных кандидата, чем 10 с мусором. "
            "Отвечай ТОЛЬКО валидным JSON без markdown."
        )

        all_cands: List[Dict] = []
        for ci, chunk in enumerate(chunks):
            user = (
                f"Тема видео: {ctx.topic or 'не задана'}.\n"
                f"Найди до {CANDIDATES_PER_CHUNK} лучших кандидатов в ЭТОМ куске. "
                f"start/end — секунды от начала ВСЕГО видео (бери из таймкодов куска). "
                f"hook — ТОЧНАЯ фраза из транскрипта, с которой должен НАЧАТЬСЯ клип.\n\n"
                "JSON строго такого вида:\n"
                '{"candidates": [{"start": 0, "end": 0, "title": "заголовок до 60 символов", '
                '"hook": "точная фраза-хук из текста", '
                '"hook_type": "вопрос|цифра|провокация|личный_опыт|контраст|петля", '
                '"scores": {"hook": 0, "flow": 0, "value": 0, "emotion": 0, "quote": 0}, '
                '"reason": "почему залетит, кратко"}]}\n\n'
                f"Кусок транскрипта:\n{chunk}"
            )
            print(f"[clip_agent] анализ куска {ci + 1}/{len(chunks)}...", flush=True)
            got: Optional[List[Dict]] = None
            for attempt in (1, 2):
                try:
                    data = llm_client.complete_json(
                        self.llm_provider, self.llm_key, system, user,
                        max_tokens=4000, temperature=0.4,
                    )
                    raw = data.get("candidates") if isinstance(data, dict) else None
                    if isinstance(raw, list):
                        got = raw
                    break
                except Exception as e:
                    if attempt == 1:
                        print(f"[clip_agent] кусок {ci + 1}/{len(chunks)}: лимит/ошибка, "
                              f"повтор через {CHUNK_RETRY_SLEEP_S}с ({str(e)[:90]})", flush=True)
                        time.sleep(CHUNK_RETRY_SLEEP_S)
                    else:
                        print(f"[clip_agent] кусок {ci + 1}/{len(chunks)} пропущен: {str(e)[:90]}")
            if got:
                all_cands.extend(got)
            if ci < len(chunks) - 1:
                time.sleep(CHUNK_SPACING_S)  # be gentle with free-tier rate limits
        return all_cands

    # ---------------------------------------------------------------- Stage B
    @staticmethod
    def _score_of(c: Dict) -> int:
        sc = c.get("scores") or {}
        total = 0.0
        for k, mx in RUBRIC_MAX.items():
            try:
                v = float(sc.get(k, 0))
            except Exception:
                v = 0.0
            total += max(0.0, min(v, mx))
        # Tolerate models that return a flat "score" instead of the rubric.
        if total <= 0:
            try:
                total = max(0.0, min(float(c.get("score", 0)), 100.0))
            except Exception:
                total = 0.0
        return int(round(total))

    @staticmethod
    def _density_features(words: List[Dict], start: float, end: float) -> Optional[Dict]:
        """Плотность речи в окне по пословным таймкодам: первое/последнее слово,
        самая длинная внутренняя пауза, темп (слов/сек). None, если words нет
        (Whisper иногда отдаёт только сегменты) → этап E просто пропускается."""
        ws: List[Tuple[float, float]] = []
        for w in words or []:
            try:
                a, b = float(w.get("start")), float(w.get("end"))
            except (TypeError, ValueError):
                continue
            if b >= start and a <= end:
                ws.append((a, b))
        if len(ws) < 2:
            return None
        ws.sort()
        max_gap = 0.0
        for (_, b0), (a1, _) in zip(ws, ws[1:]):
            max_gap = max(max_gap, a1 - b0)
        first, last = ws[0][0], ws[-1][1]
        span = max(0.1, last - first)
        return {"first": first, "last": last, "max_gap": max_gap, "wps": len(ws) / span}

    @staticmethod
    def _density_penalty(feats: Dict) -> int:
        """Штраф за «мёртвый воздух» и вялый темп (0 = идеально плотный клип)."""
        pen = 0.0
        if feats["max_gap"] > DEAD_AIR_S:
            pen += min(DEAD_AIR_PENALTY_MAX, (feats["max_gap"] - DEAD_AIR_S) * 6.0)
        if feats["wps"] < SPARSE_WPS:
            pen += 6.0
        return int(round(pen))

    @staticmethod
    def _snap_start(sentences: List[Dict], start_hint: float, hook: str) -> float:
        """Open the clip ON the hook sentence; never mid-sentence.

        Хук от модели и текст Whisper часто расходятся на слово-два, поэтому ищем
        предложение не только точным вхождением, но и по ДОЛЕ общих слов (нечётко):
        перефраз хука всё равно сажает старт на верное предложение, а не мимо.
        """
        if not sentences:
            return max(0.0, start_hint)
        hw_all = _norm_words(hook)
        hw = hw_all[:5]
        hwset = set(hw_all[:8])
        if hwset:
            order = sorted(range(len(sentences)),
                           key=lambda i: abs(sentences[i]["start"] - start_hint))
            best_i, best_key = None, None
            for i in order[:60]:  # search outward from the hint
                swords = _norm_words(sentences[i]["text"])
                exact = 1 if (hw and _contains_sub(swords, hw)) else 0
                overlap = len(hwset & set(swords)) / float(len(hwset))
                key = (exact, round(overlap, 3), -abs(sentences[i]["start"] - start_hint))
                if best_key is None or key > best_key:
                    best_key, best_i = key, i
                if exact:  # ближайшее точное вхождение — лучше не найти
                    break
            # принимаем только уверенный матч: точное вхождение ИЛИ ≥50% слов хука
            if best_i is not None and (best_key[0] == 1 or best_key[1] >= 0.5):
                return float(sentences[best_i]["start"])
        for s in sentences:  # sentence containing the hint
            if s["start"] <= start_hint < s["end"]:
                return float(s["start"])
        return float(min((s["start"] for s in sentences),  # nearest sentence start
                         key=lambda x: abs(x - start_hint)))

    @staticmethod
    def _snap_end(sentences: List[Dict], start: float, end_hint: float) -> float:
        """Конец клипа на конце предложения в окне 15–60с (предпочтительно).

        Раньше при отсутствии конца в окне возвращали None и КАНДИДАТ ОТБРАСЫВАЛСЯ —
        из-за этого на длинном видео клипов выходило мало. Теперь владельцу важно
        КОЛИЧЕСТВО, поэтому делаем мягкий рез: если чистого конца в окне нет —
        берём ближайший конец предложения в пределах потолка, иначе ровную длину.
        """
        ok = [s["end"] for s in sentences
              if start + SWEET_MIN_S <= s["end"] <= start + SWEET_MAX_S]
        if ok:
            return float(min(ok, key=lambda e: abs(e - end_hint)))
        # мягкий фолбэк: ближайший конец предложения, дающий длину в [MIN, MAX]
        soft = [s["end"] for s in sentences
                if start + MIN_CLIP_S <= s["end"] <= start + MAX_CLIP_S]
        if soft:
            return float(min(soft, key=lambda e: abs(e - (start + DEFAULT_CLIP_S))))
        return float(start + DEFAULT_CLIP_S)

    def _refine_and_rank(self, candidates: List[Dict], sentences: List[Dict],
                         duration: Optional[float], keep_n: int,
                         words: Optional[List[Dict]] = None) -> List[Dict]:
        refined: List[Dict] = []
        for c in candidates:
            if not isinstance(c, dict):
                continue
            try:
                s_hint = float(c.get("start"))
                e_hint = float(c.get("end"))
            except (TypeError, ValueError):
                continue
            if duration:                       # ИИ иногда даёт время за пределами видео
                s_hint = max(0.0, min(s_hint, duration))
                e_hint = max(0.0, min(e_hint, duration))
            if e_hint <= s_hint:
                continue
            hook = str(c.get("hook") or "").strip()
            start = self._snap_start(sentences, s_hint, hook)
            end = self._snap_end(sentences, start, e_hint)
            if end is None:
                continue   # в окне 10–60с нет конца предложения — без обрывов
            # tiny natural lead-in / tail so speech doesn't start at frame 0
            start = max(0.0, start - 0.15)
            end = end + 0.25
            if duration:
                if start >= duration - MIN_CLIP_S / 2:
                    continue
                end = min(end, duration)

            # E: плотность речи — обрезаем тишину по краям и штрафуем дыры внутри.
            score = self._score_of(c)
            feats = self._density_features(words or [], start, end)
            if feats:
                # к старту/концу подтягиваемся к реальным словам (срезаем «воздух»)
                start = max(start, feats["first"] - 0.20)
                end = min(end, feats["last"] + 0.35) if duration is None \
                    else min(end, feats["last"] + 0.35, duration)
                # пересчёт фич после обрезки + штраф за паузы/вялый темп
                feats2 = self._density_features(words or [], start, end) or feats
                score = max(0, score - self._density_penalty(feats2))

            if end - start < MIN_CLIP_S or end - start > MAX_CLIP_S + 1.0:
                continue
            refined.append({
                "start": round(start, 2),
                "end": round(end, 2),
                "title": str(c.get("title") or "").strip()[:70],
                "caption": str(c.get("title") or "").strip()[:140],
                "reason": str(c.get("reason") or "").strip()[:200],
                "score": score,
                "hook": hook[:160],
                "hook_type": str(c.get("hook_type") or "").strip().lower()[:20],
            })

        # Greedy by score: no overlaps, no near-duplicate starts.
        refined.sort(key=lambda m: m["score"], reverse=True)
        kept: List[Dict] = []
        for m in refined:
            clash = False
            for k in kept:
                ov = max(0.0, min(m["end"], k["end"]) - max(m["start"], k["start"]))
                shorter = min(m["end"] - m["start"], k["end"] - k["start"])
                if ov > 0.2 * shorter or abs(m["start"] - k["start"]) < 4.0:
                    clash = True
                    break
            if not clash:
                kept.append(m)
            if len(kept) >= keep_n:
                break
        return kept  # already in rank order (best first)

    @staticmethod
    def _slice_text(sentences: List[Dict], start: float, end: float, limit: int = 420) -> str:
        """Реальный текст клипа [start,end] для судьи — чтобы он судил СУТЬ, а не ярлык."""
        parts: List[str] = []
        for s in sentences:
            try:
                a, b = float(s["start"]), float(s["end"])
            except Exception:
                continue
            if a < end and b > start:   # реальное перекрытие, не касание границы
                t = (s.get("text") or "").strip()
                if t:
                    parts.append(t)
        return " ".join(parts)[:limit]

    @staticmethod
    def _select_diverse(ranked: List[Dict], target_count: int) -> List[Dict]:
        """Финальный отбор: тянем РАЗНЫЕ типы захода (матрица углов из контент-завода),
        но число клипов не жертвуем. Первый (топовый) всегда берётся → clip_01 = лучший."""
        if target_count <= 0:
            return []
        picked: List[Dict] = []
        leftovers: List[Dict] = []
        seen: set = set()
        for m in ranked:
            if len(picked) >= target_count:
                break
            ht = (m.get("hook_type") or "").strip().lower()
            if ht and ht in seen:
                leftovers.append(m)
                continue
            picked.append(m)
            if ht:
                seen.add(ht)
        for m in leftovers:  # добиваем слоты по рангу, если разных типов не хватило
            if len(picked) >= target_count:
                break
            picked.append(m)
        return picked[:target_count]

    @staticmethod
    def _overlaps(s: float, e: float, used: List[Tuple[float, float]], frac: float = 0.2) -> bool:
        for us, ue in used:
            ov = max(0.0, min(e, ue) - max(s, us))
            shorter = min(e - s, ue - us)
            if shorter > 0 and ov > frac * shorter:
                return True
            if abs(s - us) < 4.0:
                return True
        return False

    def _fill_to_count(self, moments: List[Dict], sentences: List[Dict],
                       duration: Optional[float], target_count: int) -> List[Dict]:
        """Добор равномерными клипами до target_count (когда LLM нашёл мало).

        Клипы садятся на границы предложений, не пересекаются с уже выбранными;
        score=50 — не «виральные», но для фермы важно КОЛИЧЕСТВО постов. clip_01
        (лучший LLM-пик) остаётся первым, добор идёт в хвост."""
        out = list(moments)
        if not duration or len(out) >= target_count:
            return out
        used: List[Tuple[float, float]] = [(m["start"], m["end"]) for m in out]
        sent_starts = sorted(float(s["start"]) for s in (sentences or [])
                             if isinstance(s.get("start"), (int, float)))

        def _try_at(pos: float) -> bool:
            if pos >= duration - MIN_CLIP_S or pos < 0:
                return False
            # старт: ближайшая граница предложения, если она РЯДОМ (<8с); иначе сырое
            # время — чтобы добор покрывал ВЕСЬ ролик, даже где транскрипт оборвался.
            start = pos
            near = [ss for ss in sent_starts if abs(ss - pos) <= 8.0]
            if near:
                start = min(near, key=lambda x: abs(x - pos))
            end = self._snap_end(sentences, start, start + DEFAULT_CLIP_S) if sentences \
                else start + DEFAULT_CLIP_S
            if not (start + MIN_CLIP_S <= end <= start + MAX_CLIP_S + 1.0):
                end = start + DEFAULT_CLIP_S
            start = max(0.0, start - 0.15)
            end = min(duration, end + 0.25)
            if (end - start) < MIN_CLIP_S or self._overlaps(start, end, used):
                return False
            txt = self._nearest_text(sentences, start) or ""
            title = " ".join(txt.split()[:8])[:70] or "Момент"
            out.append({"start": round(start, 2), "end": round(end, 2),
                        "title": title, "caption": title[:140],
                        "reason": "доборка до нужного числа клипов",
                        "score": 50, "hook": "", "hook_type": ""})
            used.append((start, end))
            return True

        # Несколько проходов равномерных позиций по ВСЕЙ длине ролика (со сдвигом),
        # чтобы клипы распределялись от начала до конца, а не лепились в начало.
        for off in (0.5, 0.25, 0.75, 0.12, 0.62, 0.37, 0.87):
            if len(out) >= target_count:
                break
            for k in range(target_count):
                if len(out) >= target_count:
                    break
                _try_at(duration * (k + off) / target_count)
        return out

    # ---------------------------------------------------------------- judge
    def _judge(self, ctx: AgentContext, pool: List[Dict], target_count: int,
               sentences: Optional[List[Dict]] = None) -> List[Dict]:
        """Final cross-chunk pass: per-chunk scores aren't calibrated against each
        other, so a separate call compares ALL finalists — НА ОСНОВЕ ИХ ТЕКСТА, не
        ярлыков — and kills the filler. Returns a RANKED list (best first), longer
        than target_count so the diversity pass can choose. On any failure returns
        the pool as-is (stage-B order)."""
        if len(pool) <= target_count:
            return pool
        sentences = sentences or []
        from . import llm_client
        try:
            lines = []
            for i, m in enumerate(pool):
                text = self._slice_text(sentences, m["start"], m["end"]) if sentences else ""
                lines.append(
                    f"id={i} | {m['start']:.0f}-{m['end']:.0f}с | балл этапа 1: {m['score']} | "
                    f"тип: {m.get('hook_type') or '—'} | хук: «{(m.get('hook') or '')[:80]}»\n"
                    f"   текст: {text or '(нет текста)'}"
                )
            want = min(len(pool), max(target_count * 2, target_count + 2))
            system = (
                "Ты — главный редактор вирусных шортсов. Тебе дают финалистов из РАЗНЫХ частей "
                "видео (их баллы ставились по отдельности и не сравнимы между собой). Читай "
                "ТЕКСТ каждого и сравнивай по сути: сильный ли хук, цельная ли мысль с выплатой, "
                "есть ли эмоция или польза. Проходные/бытовые/без хука/оборванные — отбрасывай "
                "безжалостно. Старайся, чтобы выбранные были РАЗНЫЕ по теме и типу захода, а не "
                "три почти одинаковых. Отвечай ТОЛЬКО валидным JSON."
            )
            user = (
                f"Тема видео: {ctx.topic or 'не задана'}.\n"
                f"Отбери до {want} лучших, СТРОГО от сильнейшего к слабейшему, и дай каждому "
                f"финальный балл 0-100 (строго: средний клип = 50-60, топ = 75+).\n"
                'JSON: {"picks": [{"id": 0, "score": 0}]}\n\n'
                "Финалисты:\n" + "\n".join(lines)
            )
            data = llm_client.complete_json(
                self.llm_provider, self.llm_key, system, user, max_tokens=700, temperature=0.2,
            )
            picks = data.get("picks") if isinstance(data, dict) else None
            if not isinstance(picks, list) or not picks:
                return pool
            out: List[Dict] = []
            seen = set()
            for p in picks:
                try:
                    i = int(p.get("id"))
                    sc = int(float(p.get("score", pool[i]["score"])))
                except Exception:
                    continue
                if 0 <= i < len(pool) and i not in seen:
                    seen.add(i)
                    m = dict(pool[i])
                    m["score"] = max(0, min(sc, 100))
                    out.append(m)
            return out or pool
        except Exception as e:
            print(f"[clip_agent] судья пропущен ({str(e)[:80]}) — беру порядок этапа 1")
            return pool

    # ------------------------------------------------------------------ local
    def _local(self, ctx: AgentContext, **extra: Any) -> Dict[str, Any]:
        transcript: Dict[str, Any] = extra.get("transcript") or {}
        segments: List[Dict] = transcript.get("segments") or []
        target_count: int = int(extra.get("target_count") or 3)
        duration: Optional[float] = extra.get("source_duration")

        if not duration:
            if segments:
                try:
                    duration = float(segments[-1]["end"])
                except Exception:
                    duration = None
        if not duration or duration < MIN_CLIP_S:
            return {"moments": [{
                "start": 0.0, "end": min(DEFAULT_CLIP_S, duration or DEFAULT_CLIP_S),
                "title": (ctx.topic or "TREZZY"), "caption": (ctx.topic or "TREZZY"),
                "reason": "fallback: no transcript/duration", "score": 50, "hook": "",
            }]}

        seg_starts = []
        for s in segments:
            try:
                seg_starts.append(float(s["start"]))
            except Exception:
                continue

        # Non-overlapping windows; clip length adapts to short sources.
        window = duration / target_count
        clip_len = max(MIN_CLIP_S, min(DEFAULT_CLIP_S, window))

        moments: List[Dict] = []
        for i in range(target_count):
            ws = window * i
            lo, hi = ws, ws + max(0.0, window - clip_len)
            start = ws
            if seg_starts:
                in_range = [s for s in seg_starts if lo <= s <= hi]
                if in_range:
                    start = min(in_range, key=lambda x: abs(x - ws))
            start = max(0.0, min(start, max(0.0, duration - MIN_CLIP_S)))
            end = min(duration, start + clip_len)
            if end - start < MIN_CLIP_S:
                continue
            title = self._nearest_text(segments, start) or f"{ctx.topic or 'TREZZY'} — момент {i + 1}"
            moments.append({
                "start": round(start, 2), "end": round(end, 2),
                "title": title[:70], "caption": title[:140],
                "reason": "even-spaced fallback selection", "score": 50, "hook": "",
            })
        moments = self._dedupe_overlaps(moments)
        if not moments:
            moments = [{"start": 0.0, "end": min(DEFAULT_CLIP_S, duration),
                        "title": ctx.topic or "TREZZY", "caption": ctx.topic or "TREZZY",
                        "reason": "fallback", "score": 50, "hook": ""}]
        return {"moments": moments}

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _dedupe_overlaps(moments: List[Dict]) -> List[Dict]:
        """Drop overlapping/duplicate moments — keep earliest, then the next one
        that starts at or after the last kept clip's end."""
        out: List[Dict] = []
        last_end = -1.0
        for m in sorted(moments, key=lambda x: x["start"]):
            if m["start"] >= last_end:
                out.append(m)
                last_end = m["end"]
        return out

    @staticmethod
    def _nearest_text(segments: List[Dict], t: float) -> Optional[str]:
        best = None
        best_d = 1e9
        for s in segments:
            try:
                st = float(s["start"])
            except Exception:
                continue
            d = abs(st - t)
            if d < best_d and (s.get("text") or "").strip():
                best_d = d
                best = s["text"].strip()
        return best
