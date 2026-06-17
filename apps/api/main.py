# -*- coding: utf-8 -*-
"""TREZZY Content Factory — orchestration API + dashboard host.

This service does NOT render video. It:
  1. Runs the agents to build a content plan.
  2. Calls the trezzy-video-worker over HTTP to render final.mp4.
  3. Assembles the per-job package in output/jobs/{job_id}/.
  4. Updates data/content_jobs.json.
  5. Serves the static dashboard at /.

Defaults: API on 127.0.0.1:8001, worker on 127.0.0.1:8000.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# Make the repo importable as a package root (we're a flat layout, not pip-installed).
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from packages.agents import (  # noqa: E402
    MarketingStrategistAgent,
    ScriptWriterAgent,
    VisualDirectorAgent,
    SMMCaptionAgent,
    QualityControlAgent,
    ClipAgent,
)
from packages.agents.base import AgentContext  # noqa: E402
from packages.integrations import (  # noqa: E402
    InstagramAdapter,
    TikTokAdapter,
    YouTubeAdapter,
    CapCutAdapter,
    N8nAdapter,
)
from packages.integrations.telegram_bot import (  # noqa: E402
    notify_clips_for_review, start_review_poller, review_enabled,
)
from packages.shared import (  # noqa: E402
    PlanRequest,
    GenerateFromPlanRequest,
    AccountIn,
    SettingsIn,
    JobStatus,
    JsonStore,
)
from packages.video import (  # noqa: E402
    VideoWorkerClient, WorkerUnavailable, render_fast, render_avatar, DIDError,
    render_clips, transcribe, video_duration, render_stock,
)

load_dotenv(REPO_ROOT / ".env")

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ----------------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------------
DATA_DIR     = REPO_ROOT / "data"
OUTPUT_DIR   = REPO_ROOT / "output"
JOBS_DIR     = OUTPUT_DIR / "jobs"
DASHBOARD_DIR = REPO_ROOT / "apps" / "dashboard"
ASSETS_SOURCE_DIR = REPO_ROOT / "assets" / "source"  # long videos for clip mode

DATA_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)
ASSETS_SOURCE_DIR.mkdir(parents=True, exist_ok=True)

accounts_store = JsonStore(DATA_DIR / "accounts.json")
jobs_store     = JsonStore(DATA_DIR / "content_jobs.json")
stats_store    = JsonStore(DATA_DIR / "stats.json")
products_store = JsonStore(DATA_DIR / "products.json")
settings_store = JsonStore(DATA_DIR / "settings.json")

# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------
app = FastAPI(title="TREZZY Content Factory API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _start_telegram_poller() -> None:
    # Daemon thread; reads settings fresh each cycle so token edits apply live.
    start_review_poller(lambda: settings_store.read({}))


def _utf8_json(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content=payload,
        status_code=status_code,
        media_type="application/json; charset=utf-8",
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(value: str) -> str:
    v = re.sub(r"\s+", "-", value.strip().lower())
    v = re.sub(r"[^a-z0-9а-яё\-]+", "", v, flags=re.IGNORECASE)
    return v[:40] or "topic"


def _new_job_id(topic: str) -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_slug(topic)}-{uuid.uuid4().hex[:6]}"


# ----------------------------------------------------------------------------
# Agent runner
# ----------------------------------------------------------------------------
def _build_plan(req: PlanRequest) -> Dict[str, Any]:
    """Run the agent chain and return a single plan dict."""
    settings = settings_store.read({})
    # Prefer Anthropic (better RU ad copy); fall back to OpenAI; else local templates.
    llm_provider = None
    llm_key = None
    if settings.get("anthropic_api_key"):
        llm_provider = "anthropic"
        llm_key = settings.get("anthropic_api_key")
    elif settings.get("openai_api_key"):
        llm_provider = "openai"
        llm_key = settings.get("openai_api_key")
    elif settings.get("groq_api_key"):
        llm_provider = "groq"
        llm_key = settings.get("groq_api_key")

    ctx = AgentContext(
        topic=req.topic,
        product_name=req.product_name,
        target_audience=req.target_audience,
        platform=req.platform,
        format=req.format,
        style=req.style,
        seed=req.seed,
    )

    strategist  = MarketingStrategistAgent(llm_provider=llm_provider, llm_key=llm_key)
    writer      = ScriptWriterAgent(llm_provider=llm_provider, llm_key=llm_key)
    director    = VisualDirectorAgent(llm_provider=llm_provider, llm_key=llm_key)
    smm         = SMMCaptionAgent(llm_provider=llm_provider, llm_key=llm_key)
    qc          = QualityControlAgent(llm_provider=llm_provider, llm_key=llm_key)

    strategy = strategist.run(ctx)
    script   = writer.run(ctx)
    visual   = director.run(ctx)
    caption  = smm.run(
        ctx,
        hook=script["hook"],
        script=script["script"],
        cta=script["cta"],
        vibe_tags=script["vibe_tags"],
    )
    quality  = qc.run(
        ctx,
        hook=script["hook"],
        script=script["script"],
        caption=caption["caption"],
        hashtags=caption["hashtags"],
        vibe_tags=script["vibe_tags"],
    )

    return {
        "topic":           req.topic,
        "format":          req.format,
        "platform":        req.platform,
        "product_name":    req.product_name,
        "target_audience": req.target_audience,
        "style":           req.style,
        "strategy":        strategy,
        "hook":            script["hook"],
        "script":          script["script"],
        "scenes":          script["scenes"],
        "voiceover_text":  script["voiceover_text"],
        "vibe_tags":       script["vibe_tags"],
        "cta":             script["cta"],
        "title":           script["title"],
        "visual":          visual,
        "caption":         caption["caption"],
        "hashtags":        caption["hashtags"],
        "short_title":     caption["short_title"],
        "platform_notes":  caption["platform_notes"],
        "quality":         quality,
        "created_at":      _now_iso(),
    }


# ----------------------------------------------------------------------------
# Job persistence
# ----------------------------------------------------------------------------
def _upsert_job(job: Dict[str, Any]) -> None:
    def mut(data: Dict[str, Any]):
        jobs = data.get("jobs", [])
        for i, j in enumerate(jobs):
            if j.get("job_id") == job["job_id"]:
                jobs[i] = job
                break
        else:
            jobs.append(job)
        # newest first
        jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
        data["jobs"] = jobs[:200]
        return data

    jobs_store.update(mut)


def _bump_stat_counter() -> None:
    def mut(data: Dict[str, Any]):
        totals = data.setdefault("totals", {})
        totals["videos_generated"] = int(totals.get("videos_generated", 0)) + 1
        data["updated_at"] = _now_iso()
        return data

    stats_store.update(mut)


# ----------------------------------------------------------------------------
# Clip pipeline (render_mode="clip") — repurpose a long local video into shorts.
# Fully separate from the generative path so fast/avatar/worker stay untouched.
# ----------------------------------------------------------------------------
def _llm_creds(settings: Dict[str, Any]):
    """Pick (provider, key): Anthropic → OpenAI → Groq (free) → none (templates)."""
    if settings.get("anthropic_api_key"):
        return "anthropic", settings.get("anthropic_api_key")
    if settings.get("openai_api_key"):
        return "openai", settings.get("openai_api_key")
    if settings.get("groq_api_key"):
        return "groq", settings.get("groq_api_key")
    return None, None


def _resolve_source_video(name: Optional[str]) -> Optional[Path]:
    """Resolve a clip-mode source: assets/source/<name>, repo-relative, or abs path."""
    if not name:
        return None
    for cand in (ASSETS_SOURCE_DIR / name, REPO_ROOT / name, Path(name)):
        try:
            if cand.exists() and cand.is_file():
                return cand
        except Exception:
            continue
    return None


def _write_status(job_dir: Path, status_record: Dict[str, Any]) -> None:
    (job_dir / "status.json").write_text(
        json.dumps(status_record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _upsert_job(status_record)


def _render_clip_job(req_payload: GenerateFromPlanRequest, index: int) -> Dict[str, Any]:
    """Cut a long LOCAL video into vertical shorts with face framing + captions.

    transcribe → ClipAgent (pick moments) → render_clips → package + per-clip n8n.
    Every stage degrades gracefully; the job never crashes the API.
    """
    settings = settings_store.read({})

    job_id = _new_job_id(req_payload.topic or "clip")
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    status_record: Dict[str, Any] = {
        "job_id":      job_id,
        "status":      "planned",
        "created_at":  _now_iso(),
        "format":      "clip",
        "platform":    req_payload.platform,
        "topic":       req_payload.topic or "clip",
        "package_dir": str(job_dir),
        "render_mode": "clip",
    }
    (job_dir / "request.json").write_text(
        json.dumps(req_payload.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_status(job_dir, status_record)

    # 1) Resolve source video.
    src = _resolve_source_video(req_payload.source_video)
    if not src:
        status_record.update({
            "status": "failed", "finished_at": _now_iso(),
            "error": f"source video not found: '{req_payload.source_video}'. "
                     f"Положи файл в assets/source/ и укажи его имя.",
        })
        _write_status(job_dir, status_record)
        return status_record

    status_record["status"] = "rendering"
    _write_status(job_dir, status_record)

    # 2) Transcribe (never raises; empty transcript → naive cuts, no captions).
    transcript = transcribe(src, settings)

    # 3) Pick moments via ClipAgent (LLM → local fallback, pipeline-safe).
    llm_provider, llm_key = _llm_creds(settings)
    # Длительность = РЕАЛЬНАЯ длина файла (ffmpeg-заголовок). НЕ из транскрипта:
    # на длинном видео распознавание частичное (квота) → его "duration" короче
    # реальной → клипов выходит мало. Транскрипт — только для границ/субтитров.
    duration = video_duration(src)
    if not duration and isinstance(transcript, dict):
        duration = transcript.get("duration")
    ctx = AgentContext(
        topic=req_payload.topic or src.stem,
        product_name=req_payload.product_name,
        target_audience=req_payload.target_audience,
        platform=req_payload.platform,
        format="clip",
        style=req_payload.style,
    )
    # clip_count <= 0 → auto: 2 clips per 10 minutes of source (min 2).
    target_count = req_payload.clip_count
    if not target_count or target_count <= 0:
        target_count = max(2, int(round((duration or 600) / 300.0)))
        print(f"[clip] авто-количество: {target_count} клип(ов) на {int((duration or 0) / 60)} мин")
    try:
        sel = ClipAgent(llm_provider=llm_provider, llm_key=llm_key).run(
            ctx, transcript=transcript,
            source_duration=duration, target_count=target_count,
        )
        moments = sel.get("moments", []) if isinstance(sel, dict) else []
    except Exception as e:
        print("[clip] moment selection failed entirely:", repr(e))
        moments = []

    if not moments:
        status_record.update({
            "status": "failed", "finished_at": _now_iso(),
            "error": "no moments selected (no transcript and no usable fallback). "
                     "Проверь, что в видео есть звук, или задай GROQ/OpenAI ключ.",
        })
        _write_status(job_dir, status_record)
        return status_record

    # 4) Render clips.
    try:
        result = render_clips(
            src, moments, transcript, job_dir, settings,
            meta={
                "topic":        req_payload.topic or src.stem,
                "product_name": req_payload.product_name,
                "hashtags":     [],
            },
        )
    except Exception as e:
        status_record.update({
            "status": "failed", "finished_at": _now_iso(),
            "error": f"clip render error: {e}",
        })
        _write_status(job_dir, status_record)
        return status_record

    # 5) Package + announce each clip to the publish queue (same n8n mechanism).
    (job_dir / "transcript.json").write_text(
        json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    n8n = N8nAdapter(webhook_url=settings.get("n8n_webhook_url") or "")
    payloads: List[Dict[str, Any]] = []
    for c in result.get("clips", []):
        try:
            payload = n8n.build_payload(
                job_id=f"{job_id}-clip{c['index']:02d}",
                topic=req_payload.topic or src.stem,
                format="clip",
                platform=req_payload.platform,
                hook=c.get("hook") or c.get("title") or "",
                script=c.get("reason") or "",
                caption=c.get("caption") or "",
                hashtags=[],
                cta="",
                video_path=c["path"],
                package_dir=str(job_dir),
                duration_seconds=c.get("duration"),
            )
            payloads.append(payload)
            n8n.notify(payload)   # fire-and-forget; notify() is wrapped internally
        except Exception:
            continue
    (job_dir / "n8n_payload.json").write_text(
        json.dumps({"clips": payloads}, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 5b) Telegram review: send every clip to the owner with ✅/❌ buttons.
    if review_enabled(settings):
        try:
            sent = notify_clips_for_review(
                settings, result.get("clips", []), job_id, req_payload.topic or src.stem
            )
            status_record["tg_review_sent"] = sent
            print(f"[clip] отправлено на одобрение в Telegram: {sent} клип(ов)")
        except Exception as e:
            print("[clip] telegram review send failed:", repr(e)[:160])

    final_mp4 = job_dir / "final.mp4"
    final_status = "success" if final_mp4.exists() else "failed"
    # Virality score of the best clip → the dashboard's QC badge.
    top_score = None
    for c in result.get("clips", []):
        s = c.get("score")
        if isinstance(s, (int, float)):
            top_score = max(top_score or 0, int(s))
    status_record.update({
        "status":           final_status,
        "finished_at":      _now_iso(),
        "output_path":      str(final_mp4),
        "duration_seconds": result.get("duration_seconds"),
        "render_mode":      "clip",
        "clip_count":       result.get("clip_count"),
        "source_video":     src.name,
        "transcriber":      result.get("transcriber"),
        "captions":         result.get("captions"),
    })
    if top_score is not None:
        status_record["qc_score"] = top_score
    if final_status == "failed":
        status_record["error"] = "clips finished but final.mp4 was not produced"
    _write_status(job_dir, status_record)
    if final_status == "success":
        for _ in range(int(result.get("clip_count", 1) or 1)):
            _bump_stat_counter()
    return status_record


# ----------------------------------------------------------------------------
# Job pipeline
# ----------------------------------------------------------------------------
def _render_one(req_payload: GenerateFromPlanRequest, index: int) -> Dict[str, Any]:
    # Clip mode is a fully separate path — keep the proven generative flow untouched.
    if (getattr(req_payload, "render_mode", "") or "").lower() == "clip":
        return _render_clip_job(req_payload, index)

    plan_req = PlanRequest(
        topic=req_payload.topic,
        product_name=req_payload.product_name,
        target_audience=req_payload.target_audience,
        platform=req_payload.platform,
        format=req_payload.format,
        style=req_payload.style,
        seed=(req_payload.seed + index) if req_payload.seed is not None else None,
    )
    plan = _build_plan(plan_req)

    job_id = _new_job_id(req_payload.topic)
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    status_record: Dict[str, Any] = {
        "job_id":      job_id,
        "status":      "planned",
        "created_at":  plan["created_at"],
        "format":      plan["format"],
        "platform":    plan["platform"],
        "topic":       plan["topic"],
        "package_dir": str(job_dir),
        "hashtags":    plan["hashtags"],
        "qc_score":    plan["quality"]["score"],
        "qc_ready":    plan["quality"]["ready"],
    }
    (job_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    (job_dir / "request.json").write_text(json.dumps(req_payload.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    (job_dir / "status.json").write_text(json.dumps(status_record, ensure_ascii=False, indent=2), encoding="utf-8")
    _upsert_job(status_record)

    # Render
    status_record["status"] = "rendering"
    _upsert_job(status_record)

    render_mode = (getattr(req_payload, "render_mode", "fast") or "fast").lower()

    if render_mode == "fast":
        # ---- Fast mode: render locally, in-process. Never call the HTTP worker. ----
        try:
            result = render_fast(plan, job_dir)
        except Exception as e:
            status_record.update({
                "status":      "failed",
                "finished_at": _now_iso(),
                "error":       f"Local renderer error: {e}",
            })
            (job_dir / "status.json").write_text(
                json.dumps(status_record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _upsert_job(status_record)
            return status_record
        # render_fast already wrote final.mp4 directly into job_dir.
    elif render_mode == "avatar":
        # ---- Avatar mode: talking-head via D-ID. Never call the HTTP worker. ----
        try:
            settings = settings_store.read({})
            result = render_avatar(plan, job_dir, settings)
        except DIDError as e:
            status_record.update({
                "status":      "failed",
                "finished_at": _now_iso(),
                "error":       f"D-ID avatar error: {e}",
            })
            (job_dir / "status.json").write_text(
                json.dumps(status_record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _upsert_job(status_record)
            return status_record
        except Exception as e:
            status_record.update({
                "status":      "failed",
                "finished_at": _now_iso(),
                "error":       f"Avatar render error: {e}",
            })
            (job_dir / "status.json").write_text(
                json.dumps(status_record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _upsert_job(status_record)
            return status_record
        # render_avatar already wrote final.mp4 directly into job_dir.
    elif render_mode == "real":
        # ---- Realistic mode: assemble real Pexels stock footage, in-process. ----
        try:
            settings = settings_store.read({})
            result = render_stock(plan, job_dir, settings)
        except Exception as e:
            status_record.update({
                "status":      "failed",
                "finished_at": _now_iso(),
                "error":       f"Real/stock render error: {e}",
            })
            (job_dir / "status.json").write_text(
                json.dumps(status_record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _upsert_job(status_record)
            return status_record
        # render_stock already wrote final.mp4 directly into job_dir.
    else:
        # ---- Worker mode: render via the separate HTTP video-worker. ----
        worker = VideoWorkerClient()
        try:
            result = worker.generate(
                hook=plan["hook"],
                script=plan["script"],
                cta=plan["cta"],
                title=plan["title"],
                vibe_tags=plan["vibe_tags"],
                caption=plan["caption"],
                hashtags=plan["hashtags"],
                format=plan.get("format"),
            )
        except WorkerUnavailable as e:
            status_record.update({
                "status":      "failed",
                "finished_at": _now_iso(),
                "error":       f"Video worker error: {e}",
            })
            (job_dir / "status.json").write_text(
                json.dumps(status_record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _upsert_job(status_record)
            return status_record

        # Worker writes output/latest/. Copy artefacts into our per-job dir.
        worker_pkg = Path(result.get("package_dir", ""))
        worker_mp4 = Path(result.get("output_path", ""))

        if worker_pkg.exists():
            for fname in ("final.mp4", "script.txt", "caption.txt", "hashtags.txt", "edit_notes.txt", "capcut_checklist.md", "request.json"):
                src = worker_pkg / fname
                if src.exists() and src.is_file():
                    # Don't overwrite our enriched plan.json / request.json
                    if fname == "request.json":
                        dst = job_dir / "worker_request.json"
                    else:
                        dst = job_dir / fname
                    shutil.copyfile(src, dst)

        final_mp4_w = job_dir / "final.mp4"
        if not final_mp4_w.exists() and worker_mp4.exists():
            shutil.copyfile(worker_mp4, final_mp4_w)

    final_mp4 = job_dir / "final.mp4"

    # CapCut checklist (override worker's with our richer one)
    capcut = CapCutAdapter()
    (job_dir / "capcut_checklist.md").write_text(
        capcut.build_checklist(
            hook=plan["hook"],
            cta=plan["cta"],
            format=plan["format"],
            platform=plan["platform"],
            vibe_tags=plan["vibe_tags"],
            edit_notes=plan["visual"]["capcut_edit_notes"],
        ),
        encoding="utf-8",
    )

    # Plain-text artefacts (always re-write, even if worker missed them)
    (job_dir / "script.txt").write_text(plan["script"], encoding="utf-8")
    (job_dir / "caption.txt").write_text(plan["caption"], encoding="utf-8")
    (job_dir / "hashtags.txt").write_text("\n".join(plan["hashtags"]) + "\n", encoding="utf-8")
    edit_notes_lines = [
        "TREZZY edit notes",
        "=================",
        "",
        f"Hook  : {plan['hook']}",
        f"CTA   : {plan['cta']}",
        f"Tags  : {', '.join(plan['vibe_tags'])}",
        "",
        f"Angle      : {plan['strategy']['angle']}",
        f"Emotion    : {plan['strategy']['emotion']}",
        f"Promise    : {plan['strategy']['promise']}",
        f"Objection  : {plan['strategy']['objection']}",
        f"CTA strat. : {plan['strategy']['cta_strategy']}",
        "",
        f"Background : {plan['visual']['background_mood']}",
        "Shots:",
        *[f"  - {s}" for s in plan['visual']['shot_ideas']],
        "Text layout: " + plan['visual']['text_layout_notes'],
        "",
        "CapCut edit notes:",
        *[f"  - {n}" for n in plan['visual']['capcut_edit_notes']],
    ]
    (job_dir / "edit_notes.txt").write_text("\n".join(edit_notes_lines), encoding="utf-8")

    # n8n payload
    n8n = N8nAdapter(webhook_url=settings_store.read({}).get("n8n_webhook_url") or "")
    payload = n8n.build_payload(
        job_id=job_id,
        topic=plan["topic"],
        format=plan["format"],
        platform=plan["platform"],
        hook=plan["hook"],
        script=plan["script"],
        caption=plan["caption"],
        hashtags=plan["hashtags"],
        cta=plan["cta"],
        video_path=str(final_mp4),
        package_dir=str(job_dir),
        duration_seconds=result.get("duration_seconds"),
    )
    (job_dir / "n8n_payload.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # Fire-and-forget notify, never blocks
    try:
        n8n.notify(payload)
    except Exception:
        pass

    final_status = "success" if final_mp4.exists() else "failed"
    status_record.update({
        "status":            final_status,
        "finished_at":       _now_iso(),
        "output_path":       str(final_mp4),
        "duration_seconds":  result.get("duration_seconds"),
        "render_mode":       render_mode,
    })
    if final_status == "failed":
        status_record["error"] = "render finished but final.mp4 was not produced"
    (job_dir / "status.json").write_text(json.dumps(status_record, ensure_ascii=False, indent=2), encoding="utf-8")
    _upsert_job(status_record)
    if final_status == "success":
        _bump_stat_counter()
    return status_record


# ----------------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------------
@app.get("/health")
def health():
    worker = VideoWorkerClient()
    worker_status = "down"
    try:
        h = worker.health()
        worker_status = h.get("status", "unknown")
    except WorkerUnavailable:
        worker_status = "down"

    return _utf8_json({
        "service":  "trezzy-content-factory-api",
        "status":   "ok",
        "version":  "0.1.0",
        "worker":   {"url": worker.base_url, "status": worker_status},
        "time":     _now_iso(),
    })


@app.get("/")
def root():
    index = DASHBOARD_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index), media_type="text/html; charset=utf-8")
    return _utf8_json({"service": "trezzy-content-factory-api", "status": "ok"})


@app.post("/plan")
def plan(req: PlanRequest):
    return _utf8_json(_build_plan(req))


@app.post("/generate")
def generate_passthrough(payload: Dict[str, Any]):
    """Backwards-compatible passthrough to the worker's /generate.

    Lets dashboards / n8n flows hit a single host. Required fields:
    `script` and `cta`. Optional: hook, title, vibe_tags, caption, hashtags.
    """
    if "script" not in payload or "cta" not in payload:
        raise HTTPException(status_code=400, detail="`script` and `cta` are required")
    worker = VideoWorkerClient()
    try:
        result = worker.generate(
            hook=payload.get("hook") or "",
            script=payload["script"],
            cta=payload["cta"],
            title=payload.get("title") or "TREZZY",
            vibe_tags=payload.get("vibe_tags"),
            caption=payload.get("caption"),
            hashtags=payload.get("hashtags"),
        )
    except WorkerUnavailable as e:
        raise HTTPException(status_code=502, detail=str(e))
    _bump_stat_counter()
    return _utf8_json(result)


@app.post("/generate-from-plan")
async def generate_from_plan(req: GenerateFromPlanRequest):
    """Full pipeline: agents → plan → worker render → job package."""
    loop = asyncio.get_running_loop()
    results: List[Dict[str, Any]] = []
    # Clip mode already makes clip_count clips per call; quantity would only
    # duplicate the same source, so cap clip mode to a single run.
    runs = 1 if (req.render_mode or "").lower() == "clip" else req.quantity
    for i in range(runs):
        # Render is long-running; run it in a thread so we don't starve the event loop.
        res = await loop.run_in_executor(None, _render_one, req, i)
        results.append(res)

    return _utf8_json({
        "status":  "ok",
        "count":   len(results),
        "jobs":    results,
    })


@app.get("/jobs")
def list_jobs(limit: int = 50, status: Optional[str] = None):
    data = jobs_store.read({"jobs": []})
    jobs = data.get("jobs", [])
    if status:
        jobs = [j for j in jobs if j.get("status") == status]
    return _utf8_json({"count": len(jobs), "jobs": jobs[:limit]})


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    data = jobs_store.read({"jobs": []})
    for j in data.get("jobs", []):
        if j.get("job_id") == job_id:
            # Enrich with on-disk plan if present
            job_dir = JOBS_DIR / job_id
            plan_path = job_dir / "plan.json"
            plan_obj = None
            if plan_path.exists():
                try:
                    plan_obj = json.loads(plan_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            return _utf8_json({"job": j, "plan": plan_obj, "package_dir": str(job_dir)})
    raise HTTPException(status_code=404, detail="job not found")


@app.get("/latest")
def latest():
    data = jobs_store.read({"jobs": []})
    jobs = data.get("jobs", [])
    if not jobs:
        return _utf8_json({"job": None})
    return _utf8_json({"job": jobs[0]})


@app.get("/stats")
def stats():
    return _utf8_json(stats_store.read({}))


@app.get("/products")
def products():
    return _utf8_json(products_store.read({"products": []}))


@app.get("/accounts")
def accounts_get():
    return _utf8_json(accounts_store.read({"instagram": [], "tiktok": [], "youtube": []}))


@app.post("/accounts")
def accounts_post(account: AccountIn):
    platform = account.platform.lower().strip()
    if platform not in ("instagram", "tiktok", "youtube"):
        raise HTTPException(status_code=400, detail="platform must be instagram | tiktok | youtube")

    def mut(data: Dict[str, Any]):
        bucket = data.setdefault(platform, [])
        new_entry = {
            "id":           f"{platform[:2]}_{re.sub(r'[^a-z0-9]+', '_', account.handle.lower())}_{uuid.uuid4().hex[:4]}",
            "handle":       account.handle,
            "display_name": account.display_name or account.handle,
            "status":       account.status,
            "api_key":      account.api_key,
            "notes":        account.notes or "",
        }
        # Upsert by handle
        for i, row in enumerate(bucket):
            if row.get("handle") == new_entry["handle"]:
                # Preserve id, update everything else
                new_entry["id"] = row["id"]
                bucket[i] = new_entry
                break
        else:
            bucket.append(new_entry)
        return data

    accounts_store.update(mut)
    return _utf8_json({"status": "ok", "account": account.model_dump()})


@app.get("/settings")
def settings_get():
    return _utf8_json(settings_store.read({}))


@app.post("/settings")
def settings_post(s: SettingsIn):
    def mut(data: Dict[str, Any]):
        for k, v in s.model_dump(exclude_none=True).items():
            data[k] = v
        return data

    new_settings = settings_store.update(mut)
    return _utf8_json({"status": "ok", "settings": new_settings})


@app.get("/file")
def get_file(path: str):
    """Serve a single file from output/ (read-only). Used by dashboard to
    download final.mp4 / view package files via the API.
    """
    p = Path(path).resolve()
    out_root = OUTPUT_DIR.resolve()
    # Stay inside output/
    try:
        p.relative_to(out_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="path must be inside output/")
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(str(p))


# Mount dashboard static assets last (so it doesn't shadow API routes).
if DASHBOARD_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="dashboard-static")


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("API_HOST", "127.0.0.1")
    port = int(os.getenv("API_PORT", "8001"))
    uvicorn.run("apps.api.main:app", host=host, port=port, reload=False)
