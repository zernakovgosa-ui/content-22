# TREZZY Content Factory — статус MVP

_Снимок на 2026-05-27._

## ✔ Работает локально, end-to-end

- Видео-рендер `final.mp4` (1080×1920, H.264, тёмная luxury-палитра, Cyrillic).
- API `apps/api` (FastAPI, порт 8001) с эндпоинтами:
  `/health`, `/plan`, `/generate`, `/generate-from-plan`,
  `/jobs`, `/jobs/{id}`, `/latest`,
  `/stats`, `/accounts` (GET/POST), `/settings` (GET/POST),
  `/products`, `/file`.
- Дашборд (premium dark + gold, Tailwind via CDN) — 6 вкладок:
  Главная, Создать видео, Задачи, Аккаунты, Статистика, Настройки.
- Контент-агенты (5 шт., детерминированные шаблоны):
  MarketingStrategist · ScriptWriter · VisualDirector · SMMCaption · QualityControl.
- 10 форматов: `single_review`, `top_list`, `mood_story`, `celebrity_style`,
  `problem_solution`, `luxury_quote`, `date_night`, `office_rich`,
  `quiet_luxury`, `perfume_for_mood`.
- Сборка job-пакета в `output/jobs/{job_id}/`:
  `final.mp4`, `plan.json`, `request.json`, `script.txt`, `caption.txt`,
  `hashtags.txt`, `edit_notes.txt`, `capcut_checklist.md`,
  `n8n_payload.json`, `status.json`.
- Журнал задач в `data/content_jobs.json`.
- PowerShell-скрипты: `doctor`, `start_worker`, `start_api`,
  `start_dashboard`, `start_all`, `test_full_pipeline`.

## ◐ Частично (готова структура, нужны ключи)

- LLM-режим агентов — есть `BaseAgent._llm()` точка расширения и проверка ключей
  в settings, но провайдер-специфичные вызовы (OpenAI/Anthropic SDK) не подключены.
  При отсутствии ключей всё работает на детерминированных шаблонах.
- n8n notify — webhook URL можно указать в Settings; при наличии URL API
  fire-and-forget POST-нет `n8n_payload.json`. Без URL шаг пропускается.
- Stats — структура per-platform готова, но числа `mock` до подключения
  Graph API / TikTok Data / YouTube Data.
- Voiceover — поле под ключ ElevenLabs есть в Settings, генерация audio
  не сделана (video пока без VO; добавляется в CapCut вручную).

## ✘ Mock / не реализовано (сознательно)

- Прямой постинг в Instagram / TikTok / YouTube — только адаптеры-заглушки.
  Реальный постинг требует одобренных приложений в Meta for Developers /
  TikTok for Developers / Google Cloud Console.
- CapCut автоматизация — нет официального SDK; вместо этого генерируется
  per-job `capcut_checklist.md` для редактора.
- Аутентификация / multi-user — MVP однопользовательский, привязан к localhost.

## Следующие шаги (приоритет)

1. Включить настоящий LLM-режим (`packages/agents/base.py::_llm`) — добавить
   один OpenAI вызов в каждый агент с fallback на `_local`.
2. Подключить ElevenLabs (генерация VO под `script`) и сшивать audio к
   `final.mp4` через `ffmpeg -i video -i audio` в `apps/api/main.py::_render_one`.
3. Реальный Instagram Graph API в `InstagramAdapter.publish_reel`.
4. Подключить Stats к Graph API / TikTok / YouTube — заменить mock-цифры
   реальными по `media_id` каждой опубликованной задачи.
5. Каталог `data/products.json` использовать в Create-форме (autocomplete
   по продуктам, чтобы агенты подхватывали `notes` и `vibe`).

## Что НЕ ломалось при работе

- Существующий `trezzy-video-worker/main.py` — нетронут, контракт
  `POST /generate` совместим один-в-один.
- `short-video-maker/` — нетронут.
- `output/final.mp4` всегда указывает на последний рендер (как раньше).
- Поддержка Cyrillic — сохранена (UTF-8 байты по всему пайплайну).
