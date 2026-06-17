# TREZZY Content Factory — Брифинг для Claude Code

> Этот файл — полный контекст проекта для продолжения работы в Claude Code.
> Прочитай его целиком перед любыми изменениями. Проект на Windows, путь:
> `C:\trezzy-content-factory`

---

## ЧТО ЭТО ЗА ПРОЕКТ

Локальный «контент-завод» для автоматического производства коротких вертикальных
видео (1080×1920, Reels/TikTok/Shorts) для парфюмерного бренда **TREZZY**.
Заказчик хочет видеть сквозную тех-цепочку:

**тема → сценарий (ИИ) → ролик → обработка → публикация**

Это коммерческий проект: владелец продаёт завод заказчику.

---

## АРХИТЕКТУРА (3 процесса)

1. **API + Dashboard** — FastAPI, порт **8001**, отдаёт дашборд и оркестрирует пайплайн.
   - `apps/api/main.py` — все эндпоинты + функция `_build_plan()` (агенты) и `_render_one()` (рендер).
   - `apps/dashboard/index.html` — весь UI (один файл, Tailwind через CDN, ~57KB).

2. **Video Worker** — отдельный процесс, порт **8000**, тяжёлый рендер.
   - `trezzy-video-worker/main.py` + `content_brain.py` (шаблоны сценариев — фолбэк).

3. **n8n** — порт **5678** (Docker), внешний оркестратор по расписанию.
   - ВАЖНО: из Docker наш API зовётся как `http://host.docker.internal:8001`, НЕ localhost.

### Запуск
`START.bat` — одна кнопка: убивает старый сервер (по портам 8000/8001 + все python),
чистит `__pycache__`, поднимает worker + API, открывает браузер. `STOP.bat` — остановка.

---

## ПАЙПЛАЙН АГЕНТОВ (`packages/agents/`)

Цепочка: `MarketingStrategist → ScriptWriter → VisualDirector → SMMCaption → QualityControl`.

- `base.py` — `BaseAgent`. Метод `run()` пробует `_llm()`, при любой ошибке печатает
  `[agent] LLM failed...` и падает в `_local()` (шаблон). Пайплайн НИКОГДА не ломается.
- `script_writer.py` — **главный**. `_llm()` пишет topic-first промт и зовёт OpenAI/Anthropic.
  `_local()` — фолбэк через `content_brain.make_plan()`.
- `llm_client.py` — stdlib-клиент (urllib) для OpenAI (`gpt-4o`) и Anthropic (`claude-sonnet-4-6`).
  Функции `complete()` и `complete_json()`.

### КЛЮЧЕВОЙ УРОК (почему долго мучились)
Сценарий «не слушал тему» НЕ из-за кода, а из-за трёх причин по очереди:
1. `llm_client.py` несколько раз не копировался в сборку (его просто не было → ImportError → фолбэк).
2. Старый `base.py` с `NotImplementedError` перекрывал новый.
3. **Сервер крутил старый python в памяти** — «рестарт» не убивал старый процесс,
   старый код оставался в RAM. Поэтому START.bat теперь делает HARD-KILL по портам.

Проверка что LLM реально пишет (минуя сервер):
```powershell
cd C:\trezzy-content-factory
$venv = ".\trezzy-video-worker\.venv\Scripts\python.exe"
& $venv -c "import json,sys; sys.path.insert(0,'.'); from packages.agents.script_writer import ScriptWriterAgent as S; from packages.agents.base import AgentContext as C; s=json.load(open('data/settings.json',encoding='utf-8')); print(S(llm_provider='openai',llm_key=s['openai_api_key'])._llm(C(topic='Партнёрская программа TREZZY',product_name='TREZZY'))['script'])"
```
Если печатает текст про партнёрку — LLM работает, дело только в перезапуске сервера.

---

## РЕНДЕР (`packages/video/`)

Три режима (`render_mode` в запросе):
- **fast** — `local_renderer.py`. PIL + ffmpeg (через `imageio-ffmpeg`, ставится автоматически).
  Рендерит В ПРОЦЕССЕ API (не зовёт worker). ~15 сек. Текст на премиум-фоне, Cyrillic OK.
- **avatar** — `did_client.py`. Говорящая голова через D-ID API (stdlib urllib).
  Грузит фото → POST /talks → polling → скачивает MP4. ~60 сек.
- (worker-режимы) — для будущих тяжёлых рендеров.

Вывод всегда: `output/jobs/{id}/final.mp4` + `output/latest/final.mp4`.

### D-ID — что важно знать
- Это «говорящая голова»: оживает ТОЛЬКО лицо (губы, моргание). Тело статично. Это потолок технологии.
- Модерация входного фото: bikini/очки/explicit → HTTP 451. Нужно фото анфас, одет, без очков, глаза видны.
- Бесплатный тариф = водяной знак D-ID. Платный убирает ТОЛЬКО знак, не качество.
- Фото героя: `assets/avatar/trezzy_face.png`. Голос по умолчанию `ru-RU-SvetlanaNeural` (microsoft).

---

## НАСТРОЙКИ (`data/settings.json`)

Все ключи и дефолты. Заполняются ЧЕРЕЗ дашборд (вкладка Настройки), НЕ руками.
Поля: `openai_api_key`, `anthropic_api_key`, `did_api_key`, `did_avatar_image`,
`did_voice_id`, `did_voice_provider`, дефолты бренда/стиля/платформы, `n8n_webhook_url`.
Приоритет LLM: если есть anthropic_key → он; иначе openai_key; иначе шаблоны.
Читаются СВЕЖИМИ на каждый запрос (не кешируются) — но процесс надо перезапускать после смены кода.

---

## ЭНДПОИНТЫ API (`apps/api/main.py`)
`/health /plan /generate /generate-from-plan /jobs /jobs/{id} /latest /stats
/products /accounts (GET/POST) /settings (GET/POST) /file`
- `/generate-from-plan` — сюда стучится n8n, запускает полный пайплайн.

---

## ДИЗАЙН ДАШБОРДА
Текущий стиль — **тёплый кремовый «freshcap»**: фон `#f7f4f1`, акцент моховый `#5a7d4f`,
шрифт Fraunces, мягкие карточки, скругления. Анимированный «цифровой завод» с пиксельными
человечками (стоят в простое, шагают при `.factory.running`). Классы анимации:
`.pw .bodyG .legL .legR .armL .armR .walker .clip-travel .belt-dash .reel .pub-pop`.
НЕ менять id: `factory, factory-led, factory-state-label, fx-queue, fx-done, fx-mode`.
ВАЖНО: дизайн переделывали 4 раза (тёмный → Apple → freshcap). Владельцу важно «чтобы
выглядело дорого и не уродливо». Перед большими правками дизайна — показывать результат.

---

## ЧТО УЖЕ РАБОТАЕТ ✅
- fast-рендер (текст на фоне), Cyrillic, ~15с.
- avatar-рендер через D-ID (говорящая голова).
- OpenAI-сценарии под тему (доказано прямым вызовом).
- Дашборд freshcap, адаптив, анимированный завод.
- START.bat с hard-kill (чинит «старый код в памяти»).
- n8n: готовый workflow `n8n/trezzy_pipeline.json` + гайд.
- Автономное превью для телефона (демо внешки).

## ЧТО ПРЕДСТОИТ / НЕ ДОДЕЛАНО ⏳
1. **Публикация** — сейчас только «ручная очередь». РЕШЕНО с владельцем:
   - НЕ делать автопостинг через логин/пароль (бан + риск угона).
   - Безопасно: Telegram Bot API (официальный, без банов) ИЛИ официальные API соцсетей (большой проект).
   - Адаптеры-заглушки лежат: `packages/integrations/{instagram,tiktok,youtube}_adapter.py`.
2. **Проверка n8n end-to-end** — workflow готов, но вживую сквозняком ещё не гоняли.
3. **Качество видео** — D-ID это «говорящая голова». Для динамики (как TikTok) нужны
   Veo/Kling (дорого, нестабильно) — отложено, обсуждали с владельцем.
4. **Дизайн** — владелец может снова захотеть правок. Референс — freshcap.com.

## ГРАНИЦЫ / ПРАВИЛА (важно соблюдать)
- НЕ вводить пароли от соцсетей, не строить автопостинг через логин/пароль.
- НЕ класть личные фото модели в раздаваемые архивы.
- Реалистично объяснять владельцу пределы D-ID (не обещать «кино»).

---

## TECH-ФАКТЫ
- Python venv: `trezzy-video-worker/.venv` (там стоят fastapi/uvicorn/pydantic/pillow/imageio-ffmpeg).
- Запуск API: `uvicorn apps.api.main:app --host 127.0.0.1 --port 8001` с `PYTHONPATH=<корень>`.
- Загрузки владельца на диске **D**: `D:\PC\Downloads` (НЕ C:\Users).
- Workflow-копирование больших файлов через PowerShell here-string (`@'...'@ | Out-File -Encoding utf8`)
  оказалось надёжнее, чем Expand-Archive (часто кладёт во вложенную папку и файлы «теряются»).
