# TREZZY Content Factory

Локальная фабрика премиум-контента для TREZZY — генерирует вертикальные
короткие видео (1080×1920) для TikTok / Instagram Reels / YouTube Shorts,
плюс готовый пакет для CapCut и SMM.

Всё работает локально на Windows 11, **без Docker**, **без обязательных API-ключей**.

---

## 1. Что внутри

```
C:\trezzy-content-factory
├── apps
│   ├── api           ← FastAPI оркестратор (порт 8001) + дашборд
│   └── dashboard     ← статический HTML (открывается через API)
├── packages
│   ├── agents        ← 5 контент-агентов
│   ├── video         ← клиент к видео-воркеру
│   ├── integrations  ← заглушки IG / TikTok / YT / CapCut / n8n
│   └── shared        ← схемы, утилиты
├── trezzy-video-worker  ← существующий рендер-воркер (порт 8000)
├── short-video-maker    ← внешний движок (опционально, не используется в MVP)
├── data              ← accounts.json, content_jobs.json, stats.json, products.json, settings.json
├── output            ← готовые видео и пакеты (output/jobs/{job_id}/...)
└── scripts           ← PowerShell-запускалки
```

Два процесса:
1. **Worker** (`trezzy-video-worker`) — порт `8000`, рендерит `final.mp4`.
2. **API + Dashboard** (`apps/api`) — порт `8001`, планирует, оркестрирует, отдаёт UI.

---

## 2. Что нужно поставить один раз

1. **Python 3.10 или 3.11** — [скачать](https://www.python.org/downloads/) (при установке отметь «Add Python to PATH»).
2. **FFmpeg** — открой PowerShell и выполни:
   ```powershell
   winget install Gyan.FFmpeg
   ```
   Перезапусти PowerShell, проверь: `ffmpeg -version`.

Дальше Python-зависимости поставятся автоматически при первом запуске worker.

---

## 3. Первый запуск — самый короткий путь

Открой PowerShell и выполни:

```powershell
cd C:\trezzy-content-factory

# (по желанию) проверка окружения
powershell -ExecutionPolicy Bypass -File .\scripts\doctor.ps1

# полный запуск: worker + API + дашборд + браузер
powershell -ExecutionPolicy Bypass -File .\scripts\start_all.ps1
```

Откроются два чёрных окна (worker и api) и браузер с дашбордом
**http://127.0.0.1:8001/**

При первом запуске worker создаст `.venv` и поставит зависимости — это занимает 1–3 минуты.

> Если PowerShell ругается на политику выполнения скриптов:
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
> ```

---

## 4. Как сгенерировать первое видео

В дашборде:

1. Перейди в **«Создать видео»**.
2. Заполни **Тему** (например, «Аромат для свидания»).
3. Выбери **Формат** (`date_night`, `single_review`, `quiet_luxury`, ...) и **Платформу**.
4. Жми **«Сгенерировать план»** — увидишь hook, script, caption, hashtags, QC-чеклист.
5. Жми **«Создать видео»** — рендер занимает 30–120 секунд.

После рендера в `output/jobs/{job_id}/` появится:

| Файл                  | Что это                                              |
|-----------------------|------------------------------------------------------|
| `final.mp4`           | Готовое видео 1080×1920                              |
| `plan.json`           | Полный план (агенты + visual + QC)                   |
| `request.json`        | Исходный запрос                                      |
| `script.txt`          | Текст script-а                                       |
| `caption.txt`         | Подпись для соцсети                                  |
| `hashtags.txt`        | Хэштеги построчно                                    |
| `edit_notes.txt`      | Заметки для редактора                                |
| `capcut_checklist.md` | Чек-лист для импорта в CapCut                        |
| `n8n_payload.json`    | Готовый payload для n8n HTTP node                    |
| `status.json`         | Финальный статус задачи                              |

`output/final.mp4` всегда указывает на последний рендер (для обратной совместимости с существующим worker).

---

## 5. Дашборд — что где

- **Главная** — KPI, статус сервисов, последняя задача.
- **Создать видео** — форма + результат плана / рендера.
- **Задачи** — таблица всех генераций (новые сверху).
- **Аккаунты** — IG / TikTok / YouTube, добавление токенов, статусы.
- **Статистика** — mock-данные сейчас, реальные после подключения API.
- **Настройки** — дефолты бренда, ключи (опц.), n8n webhook, CapCut notes.

Тема дашборда — премиум dark + золото. Все данные читаются/пишутся
из `data/*.json` — можно править прямо в редакторе.

---

## 6. CapCut — как передавать видео

CapCut на этом этапе **не управляется автоматически** (это требует
официального SDK, которого нет).

Воркфлоу:
1. Открой `output/jobs/{job_id}/`.
2. Открой `capcut_checklist.md` — он содержит точный чек-лист для редактора.
3. Импортируй `final.mp4` в CapCut Desktop (canvas 1080×1920).
4. Пройди по чек-листу: voiceover, music, captions, переходы, экспорт.

---

## 7. n8n — как дёргать локальный API

API готов принимать запросы из n8n HTTP Request node.

**URL:** `http://127.0.0.1:8001/generate-from-plan`

**Если n8n в Docker на той же машине:** `http://host.docker.internal:8001/generate-from-plan`

**Method:** `POST`, **Content-Type:** `application/json; charset=utf-8`

**Body:**

```json
{
  "topic": "Аромат для свидания",
  "format": "date_night",
  "product_name": "TREZZY Date Night",
  "target_audience": "мужчины 25-35",
  "platform": "instagram",
  "quantity": 1
}
```

**Timeout:** ставь `300000` мс (5 минут) — рендер медленный.

В **Settings → n8n webhook URL** можно указать URL обратного webhook —
после готовности задачи API сам POST-нет туда `n8n_payload.json`
(fire-and-forget, не блокирует генерацию).

---

## 8. API endpoints

| Method | Path                       | Назначение                                  |
|--------|----------------------------|---------------------------------------------|
| GET    | `/health`                  | статус API + worker                         |
| POST   | `/plan`                    | план без рендера (агенты → JSON)            |
| POST   | `/generate-from-plan`      | полный пайплайн (план → рендер → пакет)     |
| POST   | `/generate`                | прямой проброс в worker (легаси-схема)      |
| GET    | `/jobs`                    | список задач                                |
| GET    | `/jobs/{id}`               | детали задачи + план                        |
| GET    | `/latest`                  | последняя задача                            |
| GET    | `/stats`                   | статистика (mock)                           |
| GET    | `/accounts`                | аккаунты                                    |
| POST   | `/accounts`                | добавить / обновить аккаунт                 |
| GET    | `/settings`                | настройки                                   |
| POST   | `/settings`                | сохранить настройки                         |
| GET    | `/products`                | каталог продуктов                           |
| GET    | `/file?path=...`           | скачать файл из `output/` (read-only)       |

---

## 9. Что mock, а что реально

**Сейчас работает по-настоящему:**
- Рендер `final.mp4` через worker (Pillow / moviepy / numpy).
- Все 10 форматов (5 старых + 5 новых: `luxury_quote`, `date_night`, `office_rich`, `quiet_luxury`, `perfume_for_mood`).
- 5 агентов (MarketingStrategist, ScriptWriter, VisualDirector, SMMCaption, QualityControl) — детерминированные шаблоны.
- Сборка job-пакета в `output/jobs/{id}/`.
- Журнал задач в `data/content_jobs.json`.
- Дашборд: создание плана, рендер, просмотр задач, аккаунтов, статистики, настроек.

**Требует реальных API-ключей (заглушки сейчас):**
- Постинг в Instagram / TikTok / YouTube — нужны токены Graph API / Content Posting API / Data API v3.
- Статистика по платформам — сейчас mock, после интеграции пойдут реальные цифры.
- Voiceover ElevenLabs — заглушка, ключ можно вписать в Settings.
- LLM-режим агентов — добавь OpenAI или Anthropic ключ в Settings, агенты автоматически переключатся (когда `_llm()` будет дописан под выбранный SDK).

CapCut всегда «manual» — это сознательное решение, переписывать его на API нечем.

---

## 10. Если что-то сломалось

- `scripts\doctor.ps1` — покажет, чего не хватает.
- Worker не отвечает на `http://127.0.0.1:8000/health` → перезапусти `start_worker.ps1`.
- API не отвечает на `http://127.0.0.1:8001/health` → перезапусти `start_api.ps1`.
- Кириллица в логах превратилась в `?` — проверь, что окно PowerShell в UTF-8 (`chcp 65001`).
- Дашборд белый/без стилей → проверь интернет: Tailwind грузится с CDN.

Подробный статус MVP: `PROJECT_STATUS.md`.
