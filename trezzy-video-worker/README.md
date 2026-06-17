# TREZZY Content Factory — Video Worker

Local Python service that generates a vertical **1080 × 1920** premium-perfume short
and a full hand-off package for editing / posting — without Docker.

Stack: **Python + FastAPI + moviepy + Pillow + numpy + python-dotenv**

The project folder is `c:\trezzy-content-factory\trezzy-video-worker`.

---

## 1. Requirements

- **Python 3.10 or 3.11**
- **FFmpeg** on PATH

Check FFmpeg:

```powershell
ffmpeg -version
```

If FFmpeg is missing:

```powershell
winget install Gyan.FFmpeg
```

Then restart PowerShell.

---

## 2. First-time setup (Windows PowerShell)

```powershell
cd c:\trezzy-content-factory\trezzy-video-worker
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If `Activate.ps1` is blocked by policy, run once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

---

## 3. Run the server

```powershell
cd c:\trezzy-content-factory\trezzy-video-worker
.\.venv\Scripts\Activate.ps1
python main.py
```

The server starts at **http://127.0.0.1:8000**.

Quick check: open http://127.0.0.1:8000/health — it should return
`{"status":"ok","service":"trezzy-video-worker"}`.

---

## 4. Send a test request

In a **second** PowerShell window:

```powershell
cd c:\trezzy-content-factory\trezzy-video-worker
.\test_request.ps1
```

After ~60–120s the package is written to `output\latest\` and Explorer opens.

---

## 5. API

### `GET /health`

```json
{ "status": "ok", "service": "trezzy-video-worker" }
```

### `POST /generate`

Full request body:

```json
{
  "hook": "Аромат, который пахнет дорого",
  "title": "TREZZY",
  "script": "Это аромат для тех, кто хочет выглядеть спокойно, дорого и уверенно. Он не кричит, но его точно запоминают.",
  "vibe_tags": ["спокойствие", "дорогой шлейф", "уверенность"],
  "cta": "Найди свой аромат на TREZZY",
  "caption": "Аромат, который не кричит. Но его запоминают.",
  "hashtags": ["#trezzy", "#parfum", "#niche"]
}
```

Only `script` and `cta` are required. `caption` and `hashtags` are generated if missing.

Response:

```json
{
  "status": "success",
  "output_path": "c:\\trezzy-content-factory\\trezzy-video-worker\\output\\final.mp4",
  "package_dir": "c:\\trezzy-content-factory\\trezzy-video-worker\\output\\latest",
  "duration_seconds": 13.9,
  "created_at": "2026-05-27T00:00:00+00:00",
  "caption": "...",
  "hashtags": ["#trezzy", "..."]
}
```

### Output package — `output/latest/`

| File                  | What it is                                                |
| --------------------- | --------------------------------------------------------- |
| `final.mp4`           | Generated 1080×1920 video (also mirrored to `output/`)    |
| `script.txt`          | Plain script text                                         |
| `caption.txt`         | Social caption (provided or auto-generated)               |
| `hashtags.txt`        | One hashtag per line                                      |
| `edit_notes.txt`      | TREZZY style guide + format-specific guidance             |
| `capcut_checklist.md` | CapCut import + finishing checklist                       |
| `request.json`        | Normalized request, for audit / replay                    |
| `plan.json`           | (only when called via `/generate-from-plan`) full brief   |

`output/final.mp4` is always kept as the canonical latest video (backward compatible).

### `POST /plan` — content brain

Turn a simple topic into a complete TREZZY plan. No video render.

```json
{
  "topic": "Аромат для свидания",
  "product_name": null,
  "target_audience": "мужчины 20-35",
  "style": "premium luxury perfume",
  "format": "single_review",
  "seed": null
}
```

Only `topic` is required. Supported `format` values:

| Format             | Use it for                                  |
| ------------------ | ------------------------------------------- |
| `single_review`    | Review of one fragrance                     |
| `top_list`         | Top-3 fragrance pick                        |
| `mood_story`       | Atmospheric / cinematic story               |
| `celebrity_style`  | Archetype / icon-style fragrance            |
| `problem_solution` | Problem → fragrance as the answer           |
| `luxury_quote`     | Short typographic luxury quote video        |
| `date_night`       | Date-night perfume scene                    |
| `office_rich`      | Office / boardroom signature scent          |
| `quiet_luxury`     | Quiet-luxury minimalist aesthetic           |
| `perfume_for_mood` | Pick a fragrance to match a mood            |

Response:

```json
{
  "hook": "...",
  "title": "TREZZY",
  "script": "...",
  "vibe_tags": ["...", "...", "..."],
  "cta": "Найди свой аромат на TREZZY",
  "caption": "...",
  "hashtags": ["#trezzy", "..."],
  "edit_notes": "Single review: фокус на одном флаконе ...",
  "format": "single_review",
  "topic": "Аромат для свидания"
}
```

Pass `seed: 42` (or any int) to get a reproducible plan.

### `POST /generate-from-plan` — plan + render in one call

Same body as `/plan`. The worker plans, renders the video, writes the full
package, and returns the plan together with the usual generation response:

```json
{
  "status": "success",
  "output_path": "...\\output\\final.mp4",
  "package_dir": "...\\output\\latest",
  "duration_seconds": 13.9,
  "created_at": "2026-05-27T00:00:00+00:00",
  "caption": "...",
  "hashtags": ["..."],
  "plan": { /* full /plan response */ }
}
```

---

## 6. Project structure

```
trezzy-content-factory/trezzy-video-worker/
├── main.py
├── requirements.txt
├── README.md
├── content_brain.py         # local template-driven planner
├── sample_request.json
├── sample_plan.json
├── test_request.ps1
├── test_plan.ps1
├── test_generate_from_plan.ps1
├── .env.example
├── .gitignore
├── assets/
│   ├── backgrounds/  (drop branded images here)
│   ├── perfume/      (product cutouts)
│   ├── overlays/     (light leaks, film grain)
│   ├── music/        (ambient tracks)
│   └── fonts/        (custom typography)
└── output/
    ├── final.mp4               # canonical latest, always present
    └── latest/                 # full hand-off package
        ├── final.mp4
        ├── script.txt
        ├── caption.txt
        ├── hashtags.txt
        ├── edit_notes.txt
        ├── capcut_checklist.md
        └── request.json
```

---

## 7. n8n integration

The worker is designed to be triggered from n8n as a local HTTP service so an
automation flow can produce a video + caption + hashtags from one row in a
content sheet.

### 7.1 Local worker URL

When the server is running on this machine:

```
http://127.0.0.1:8000
```

If n8n runs on the **same machine** as the worker, use `127.0.0.1`.
If n8n runs in Docker on the same machine, use `http://host.docker.internal:8000`
from inside the container.

### 7.2 Health node — verify the worker is up

Add an **HTTP Request** node:

| Field         | Value                                |
| ------------- | ------------------------------------ |
| Method        | `GET`                                |
| URL           | `http://127.0.0.1:8000/health`       |
| Response      | JSON                                 |

Expected: `{ "status": "ok", "service": "trezzy-video-worker" }`. Use this as
a precondition before triggering generation.

### 7.3 Brief → plan + render (`/generate-from-plan`)

If the upstream node only has a topic (a content sheet row, a Telegram message),
point n8n at `/generate-from-plan` and let the worker write the script for you.

| Field         | Value                                                         |
| ------------- | ------------------------------------------------------------- |
| Method        | `POST`                                                        |
| URL           | `http://127.0.0.1:8000/generate-from-plan`                    |
| Body          | JSON                                                          |

```json
{
  "topic":           "{{$json[\"topic\"]}}",
  "product_name":    "{{$json[\"product_name\"]}}",
  "target_audience": "{{$json[\"audience\"]}}",
  "format":          "single_review"
}
```

The response includes both the rendered package paths AND the plan, so n8n can
post the caption to your CMS in the same flow.

### 7.4 Generate node — full-control short

| Field         | Value                                                         |
| ------------- | ------------------------------------------------------------- |
| Method        | `POST`                                                        |
| URL           | `http://127.0.0.1:8000/generate`                              |
| Authentication| None                                                          |
| Send Body     | On                                                            |
| Body Type     | JSON                                                          |
| Specify Body  | JSON                                                          |

JSON body (use expressions to inject from previous nodes):

```json
{
  "hook":      "{{$json[\"hook\"]}}",
  "title":     "TREZZY",
  "script":    "{{$json[\"script\"]}}",
  "vibe_tags": ["{{$json[\"v1\"]}}", "{{$json[\"v2\"]}}", "{{$json[\"v3\"]}}"],
  "cta":       "{{$json[\"cta\"]}}",
  "caption":   "{{$json[\"caption\"]}}",
  "hashtags":  ["#trezzy", "#parfum"]
}
```

Important options:

- Set **Timeout** to at least `180000` ms (3 min) — rendering is slow.
- Set **Body Content-Type** header to `application/json; charset=utf-8`
  so Cyrillic is preserved.

### 7.5 Downstream nodes

From the response you can fork:

- `output_path` → upload `final.mp4` to Google Drive / S3 / Telegram.
- `caption`     → push to Notion or your CMS draft row.
- `hashtags`    → join with space and attach to the caption for TikTok / Reels.
- `package_dir` → list files and ship the whole bundle to the editor.

### 7.6 Curl smoke test (outside n8n)

```bash
curl -X POST http://127.0.0.1:8000/generate \
  -H "Content-Type: application/json; charset=utf-8" \
  --data-binary @sample_request.json
```
