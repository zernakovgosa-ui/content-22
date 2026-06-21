# -*- coding: utf-8 -*-
"""Tiny LLM client used by agents to generate copy.

Supports Anthropic (Claude) and OpenAI via their HTTP APIs, stdlib only
(urllib) so it runs on Windows without extra installs. Returns plain text.

Used by ScriptWriterAgent etc. via BaseAgent._llm(). If the call fails for
any reason, the caller falls back to local templates — the pipeline never
breaks.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
import urllib.error
from typing import Optional

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"  # OpenAI-compatible

# Reasonable, current default models. Override via settings if needed later.
ANTHROPIC_MODEL = "claude-sonnet-4-6"
OPENAI_MODEL = "gpt-4o"
GROQ_MODEL = "llama-3.3-70b-versatile"  # free tier, strong, multilingual (RU ok)
# Gemini через OpenAI-совместимый эндпоинт Google — бесплатный ключ на aistudio.google.com,
# умнее бесплатного Groq, щедрый дневной лимит. Модель меняется в settings (gemini_model).
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_MODEL = "gemini-2.5-flash"


class LLMError(Exception):
    pass


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"  # plain urllib UA gets 403'd by Cloudflare (Groq etc.)


def _extract_text(res: dict) -> str:
    """Текст из OpenAI-совместимого ответа (choices[0].message.content). Пусто, если
    ответ без choices/контента — единая точка, чтобы и complete(), и пул Groq
    одинаково отличали валидный ответ от пустого 200."""
    choices = res.get("choices") if isinstance(res, dict) else None
    if not choices:
        return ""
    return ((choices[0].get("message", {}) or {}).get("content") or "").strip()


def _post(url: str, headers: dict, payload: dict, timeout: int = 60) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {**headers, "User-Agent": USER_AGENT}
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    # Сеть до Groq/OpenAI бывает нестабильной (SSL handshake timeout) — транзиентные
    # сбои и 429/5xx ретраим до 3 раз, прочие HTTP-ошибки отдаём сразу.
    last_err = ""
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                last_err = f"HTTP {e.code}"
                time.sleep(6 * attempt)
                continue
            raise LLMError(f"HTTP {e.code}: {detail or e.reason}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = str(e)
            if attempt < 3:
                time.sleep(5 * attempt)
                continue
        except ValueError as e:   # 200, но тело не JSON (HTML-заглушка CF/шлюза) → ретрай
            last_err = f"не-JSON ответ ({str(e)[:50]})"
            if attempt < 3:
                time.sleep(5 * attempt)
                continue
    raise LLMError(f"unreachable after 3 tries: {last_err}")


def _post_groq(payload: dict, fallback_key: str = "", timeout: int = 60) -> dict:
    """POST к Groq с РОТАЦИЕЙ ключей: при 429 (лимит/квота) сразу берём следующий
    ключ из общего пула и повторяем. Запасной — переданный fallback_key."""
    try:
        from . import groq_pool
    except Exception:
        groq_pool = None
    pool_keys = groq_pool.keys() if groq_pool else []
    keys = pool_keys or ([fallback_key] if fallback_key else [])
    if not keys:
        raise LLMError("groq: нет ключа")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_err = ""
    tries = len(keys) + 2
    for t in range(tries):
        key = groq_pool.current() if (groq_pool and pool_keys) else keys[t % len(keys)]
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                   "User-Agent": USER_AGENT}
        req = urllib.request.Request(GROQ_URL, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = json.loads(r.read().decode("utf-8"))
            if not _extract_text(res) and t < tries - 1:   # 200 с пустым content (бывает на free Groq)
                last_err = f"empty 200 (ключ …{key[-4:]})"
                if groq_pool and pool_keys and groq_pool.count() > 1:
                    groq_pool.rotate()
                print("[groq] пустой ответ (200) — следующий ключ", flush=True)
                continue
            return res
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            if e.code in (429, 401, 403):           # лимит ИЛИ битый/запрещённый ключ → следующий
                last_err = f"HTTP {e.code} (ключ …{key[-4:]})"
                if groq_pool and pool_keys and groq_pool.count() > 1:
                    groq_pool.rotate()
                print(f"[groq] HTTP {e.code} (лимит/битый ключ) — следующий ключ", flush=True)
                if t >= len(keys) - 1 and e.code == 429:   # обошли все → пауза (только при лимите)
                    time.sleep(5)
                continue
            if e.code in (500, 502, 503, 504) and t < tries - 1:
                last_err = f"HTTP {e.code}"
                time.sleep(4)
                continue
            raise LLMError(f"HTTP {e.code}: {detail or e.reason}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = str(e)
            if t < tries - 1:
                time.sleep(4)
                continue
        except ValueError as e:   # 200, но тело не JSON → следующий круг/ключ
            last_err = f"не-JSON ответ ({str(e)[:50]})"
            if t < tries - 1:
                time.sleep(4)
                continue
    raise LLMError(f"groq unreachable after rotation: {last_err}")


def complete(
    provider: str,
    api_key: str,
    system: str,
    user: str,
    model: Optional[str] = None,
    max_tokens: int = 1200,
    temperature: float = 0.8,
) -> str:
    """Return the model's text completion. Raises LLMError on failure."""
    if provider == "anthropic":
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": model or ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        res = _post(ANTHROPIC_URL, headers, payload)
        parts = res.get("content", [])
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        if not text:
            raise LLMError(f"empty Anthropic response: {res}")
        return text.strip()

    if provider in ("openai", "groq", "gemini"):
        # Все говорят на OpenAI chat-completions схеме — различаются URL + модель.
        default_model = {"openai": OPENAI_MODEL, "groq": GROQ_MODEL, "gemini": GEMINI_MODEL}[provider]
        payload = {
            "model": model or default_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if provider == "groq":
            res = _post_groq(payload, fallback_key=api_key)   # пул ключей + ротация при 429
        elif provider == "gemini":
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            res = _post(GEMINI_URL, headers, payload)
        else:
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            res = _post(OPENAI_URL, headers, payload)
        text = _extract_text(res)
        if not text:
            # Пустой content (бывает на free Groq) — НЕ валидный ответ. Бросаем,
            # чтобы сработал ретрай/фолбэк, а не пустой текст.
            raise LLMError(f"empty {provider} response: {str(res)[:300]}")
        return text

    raise LLMError(f"unknown provider: {provider}")


def complete_json(
    provider: str,
    api_key: str,
    system: str,
    user: str,
    **kw,
) -> dict:
    """Like complete(), but parse the result as JSON (strips code fences)."""
    raw = complete(provider, api_key, system, user, **kw)
    cleaned = raw.strip()
    # Снять ```...```-фенс ГДЕ УГОДНО (модели часто пишут пояснение ДО блока, с
    # тегом языка или без) — берём содержимое первого код-блока.
    m = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S | re.I)
    if m:
        cleaned = m.group(1).strip()
    cleaned = cleaned.strip().strip("`").strip()
    try:
        return json.loads(cleaned)
    except Exception:
        # Последний шанс: вырезать от первой { или [ до парной закрывающей.
        for op, cl in (("{", "}"), ("[", "]")):
            i, j = cleaned.find(op), cleaned.rfind(cl)
            if 0 <= i < j:
                try:
                    return json.loads(cleaned[i:j + 1])
                except Exception:
                    pass
        raise LLMError(f"bad JSON from model: raw={raw[:300]}")


__all__ = ["complete", "complete_json", "LLMError"]
