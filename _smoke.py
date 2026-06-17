# Smoke test — verifies content_brain + agents + api importability + plan generation.
# Does NOT render video. Does NOT start any server. Safe to run.
import sys, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "trezzy-video-worker"))

print("=== content_brain ===")
import content_brain as cb
fmts = sorted(cb.SUPPORTED_FORMATS)
print(f"formats ({len(fmts)}):", fmts)
for f in fmts:
    p = cb.make_plan(topic="Аромат для тестов", format=f, seed=42)
    assert p["hook"] and p["script"] and p["cta"] and p["hashtags"], f"empty fields in {f}"
    print(f"  [{f:18s}] hook = {p['hook'][:48]!r}")

print()
print("=== agents ===")
from packages.agents import (
    MarketingStrategistAgent, ScriptWriterAgent, VisualDirectorAgent,
    SMMCaptionAgent, QualityControlAgent,
)
from packages.agents.base import AgentContext

ctx = AgentContext(
    topic="Аромат для свидания",
    product_name="TREZZY Date Night",
    target_audience="мужчины 25-35",
    platform="instagram",
    format="date_night",
    seed=42,
)

strategist = MarketingStrategistAgent().run(ctx)
writer     = ScriptWriterAgent().run(ctx)
visual     = VisualDirectorAgent().run(ctx)
smm        = SMMCaptionAgent().run(ctx,
    hook=writer["hook"], script=writer["script"], cta=writer["cta"],
    vibe_tags=writer["vibe_tags"])
qc         = QualityControlAgent().run(ctx,
    hook=writer["hook"], script=writer["script"], caption=smm["caption"],
    hashtags=smm["hashtags"], vibe_tags=writer["vibe_tags"])

print(f"strategist.angle    = {strategist['angle'][:60]!r}")
print(f"writer.hook         = {writer['hook'][:60]!r}")
print(f"writer.scenes       = {len(writer['scenes'])} scene(s)")
print(f"visual.shot_ideas   = {len(visual['shot_ideas'])} shots")
print(f"smm.short_title     = {smm['short_title'][:60]!r}")
print(f"smm.hashtags        = {smm['hashtags'][:5]}")
print(f"qc.score / ready    = {qc['score']} / {qc['ready']}")
print(f"qc.checks           = {len(qc['checks'])}, warnings = {len(qc['warnings'])}")

print()
print("=== integrations ===")
from packages.integrations import (
    InstagramAdapter, TikTokAdapter, YouTubeAdapter, CapCutAdapter, N8nAdapter,
)
ig = InstagramAdapter(); print("ig.status      =", ig.status())
tt = TikTokAdapter();    print("tt.status      =", tt.status())
yt = YouTubeAdapter();   print("yt.status      =", yt.status())
cc = CapCutAdapter().build_checklist(
    hook=writer["hook"], cta=writer["cta"], format=ctx.format,
    platform=ctx.platform, vibe_tags=writer["vibe_tags"],
    edit_notes=visual["capcut_edit_notes"],
)
print("capcut chklist =", f"{len(cc)} chars")
n8 = N8nAdapter().build_payload(
    job_id="test-001", topic=ctx.topic, format=ctx.format, platform=ctx.platform,
    hook=writer["hook"], script=writer["script"], caption=smm["caption"],
    hashtags=smm["hashtags"], cta=writer["cta"],
    video_path="output/final.mp4", package_dir="output/jobs/test-001",
    duration_seconds=14.2,
)
print("n8n payload    =", list(n8.keys()))

print()
print("=== shared storage ===")
from packages.shared import JsonStore
s = JsonStore(ROOT / "data" / "settings.json")
sd = s.read()
print("settings keys  =", list(sd.keys())[:5], "...")

print()
print("=== apps.api importable ===")
import apps.api.main as api_mod
print("app =", api_mod.app)
routes = sorted({r.path for r in api_mod.app.routes if hasattr(r, "path")})
print("routes:", routes)

print()
print("ALL SMOKE CHECKS PASSED ✓")
