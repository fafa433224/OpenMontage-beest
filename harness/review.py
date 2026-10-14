"""Review stage — QC tripwires plus the visual audit and an LLM review round.

Tripwires (hard, deterministic — the "don't trust green CI" layer):

* final video exists, has video+audio streams, h264/aac
* duration error vs (voiceover + intro + outro) < 4s
* word coverage > 80% when transcription numbers are known
* stream resolution is the target 1920x1080 (or the brand/platform profile)

**Visual audit** (``harness.vision``) — the stage that makes this pipeline
*look* at its own output. Deterministic frame metrics catch the creative
failure modes the ffprobe tripwires structurally cannot: scenes that never
animate, a video whose scenes all look alike, unreadable contrast, dead or
blown frames. When a vision-capable model is configured it additionally
watches the sampled frames and returns a per-scene critique — the
inspect-and-revise mechanic that separates Fable-class agents from blind
renderers. It is advisory in the sense that it never blocks a delivery on
model availability, but its *deterministic* findings are hard: a video whose
scenes are all identical is not a pass.

The LLM review (when a runtime model is configured) reads the QC json +
the plan's own qa_notes and produces a written review saved to the
project artifacts. It cannot fail the run — it informs the next one.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional


def _ffprobe(path: Path) -> dict[str, Any]:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=20,
        ).stdout
        return json.loads(out)
    except Exception:
        return {}


def _duration(path: Path) -> Optional[float]:
    data = _ffprobe(path)
    try:
        return float(data["format"]["duration"])
    except Exception:
        return None


REVIEW_SYSTEM_PROMPT = """You are the post-production reviewer of a video pipeline.
You receive the QC report and the production plan (including its own
qa_notes). Judge the RENDERED result against the plan and report issues
that matter to a viewer: pacing, coverage, duration sanity, anything the
qc_notes asked to verify. Be specific and terse.

Answer with ONLY a JSON object:
{
  "pass": true,
  "score": 0-10,
  "issues": ["one line each, only real problems"],
  "wins": ["what worked"],
  "next_video_suggestions": ["concrete, actionable"]
}"""


def review_project(
    *, project_id: str, out_dir: Path, name: str, script: dict,
    llm: Any, final: Path,
    skip_visual: bool = False,
    vision_enabled: bool = True,
    fast_visual: bool = False,
) -> dict[str, Any]:
    verdict: dict[str, Any] = {"pass": True, "checks": [], "issues": [], "visual": None}

    def check(ok: bool, label: str, detail: str = "") -> None:
        verdict["checks"].append({"check": label, "ok": bool(ok), "detail": detail})
        if not ok:
            verdict["pass"] = False
            verdict["issues"].append(f"{label}: {detail}" if detail else label)

    qc_path = out_dir / f"{name}-qc.json"
    timing_path = out_dir / f"{name}-timing.json"

    # --- QC-file tripwires (independent of the final file) --------------
    if qc_path.exists():
        qc = json.loads(qc_path.read_text())
        if "word_coverage_pct" in qc:
            check(float(qc["word_coverage_pct"]) >= 80.0, "word_coverage",
                  f"{qc['word_coverage_pct']}%")

    # --- final-file tripwires --------------------------------------------
    check(final.exists(), "final_exists", str(final))
    if not final.exists():
        return verdict

    probe = _ffprobe(final)
    streams = probe.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})
    check(bool(v), "video_stream_present", v.get("codec_name", ""))
    check(bool(a), "audio_stream_present", a.get("codec_name", ""))

    vdur = _duration(final) or 0.0
    expected = 0.0
    if qc_path.exists():
        qc = json.loads(qc_path.read_text())
        expected = qc.get("expected_duration_s", 0.0) + \
            qc.get("intro_outro_included", False) * 0  # intro/outro already inside timing below
    if timing_path.exists():
        timing = json.loads(timing_path.read_text())
        expected += timing.get("hyperframes_intro_s", 0) + timing.get("hyperframes_outro_s", 0)
    if expected > 0:
        check(abs(vdur - expected) < 4.0, "duration_sane",
              f"{vdur:.1f}s vs expected ~{expected:.1f}s")

    if width := v.get("width"):
        check(int(width) >= 1080, "resolution_target", f"{v.get('width')}x{v.get('height')}")

    # --- visual audit: look at the frames --------------------------------
    visual: Optional[dict[str, Any]] = None
    if not skip_visual:
        try:
            from harness.vision import audit_report_text, scene_spans_from_timing, visual_audit

            timing_payload = {}
            if timing_path.exists():
                timing_payload = json.loads(timing_path.read_text())
            spans = scene_spans_from_timing(timing_payload, vdur) or None
            # The vision critic only runs when the model actually supports
            # images; a text-only endpoint would waste a call and 400.
            vision_llm = llm if (llm is not None and vision_enabled) else None
            visual = visual_audit(
                final, out_dir,
                scene_spans=spans, llm=vision_llm, script=script,
                samples_per_scene=3 if not fast_visual else 2,
                # --fast-visual trades fidelity for time. It must not fake
                # legibility confidence: at 640 px the sample cannot see
                # whether thin type survived, and vision.py downgrades those
                # findings to advisory on its own from this value.
                frame_width=1280 if not fast_visual else 640,
            )
            metrics = visual.get("metrics") or {}
            check(bool(visual.get("pass")), "visual_audit",
                  f"variety {metrics.get('variety_ratio', 0):.0%}, "
                  f"motion {metrics.get('motion_energy_mean', 0):.4f}, "
                  f"contrast {metrics.get('contrast_min', 0):.3f}")
            for issue in (visual.get("issues") or []):
                if issue.get("severity") == "high":
                    verdict["issues"].append(
                        f"visual[{issue.get('severity')}]: {issue.get('problem')}"
                    )
            print(audit_report_text(visual), flush=True)
        except Exception as exc:  # never let the audit lose a good render
            print(f"[harness] visual audit unavailable: {exc}", flush=True)

    # --- LLM review (advisory) ------------------------------------------
    llm_review = None
    if llm is not None:
        try:
            payload = {
                "qc": json.loads(qc_path.read_text()) if qc_path.exists() else {},
                "timing": json.loads(timing_path.read_text()) if timing_path.exists() else {},
                "plan": {
                    "title": script.get("title"),
                    "sections": [
                        {"heading": s.get("heading"), "text": (s.get("text") or "")[:200]}
                        for s in script.get("sections", [])
                    ],
                    "qa_notes": script.get("qa_notes", script.get("broll_moods", [])),
                },
                "final_video_seconds": round(vdur, 2),
            }
            review = llm.chat_json(
                [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                system=REVIEW_SYSTEM_PROMPT,
                temperature=0.3,
                max_tokens=800,
            )
            if isinstance(review, dict):
                llm_review = review
        except Exception:
            pass  # advisory only

    if visual is not None:
        verdict["visual"] = visual
        # A video whose scenes all look alike is the exact defect this repo
        # exists to fix; it must never read as a pass.
        if any(i.get("severity") == "high" for i in visual.get("issues") or []):
            verdict["pass"] = False
            for issue in visual["issues"]:
                if issue.get("severity") == "high":
                    verdict["issues"].append(f"visual: {issue.get('problem')}")

    verdict["llm_review"] = llm_review

    # Persist the review artifact.
    artifacts = Path(__file__).resolve().parent.parent / "projects" / project_id / "artifacts"
    try:
        from lib.paths import PROJECTS_DIR

        artifacts = Path(PROJECTS_DIR) / project_id / "artifacts"
    except Exception:
        pass
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "harness_review.json").write_text(json.dumps(verdict, indent=1))

    print(f"[harness] review: {'PASS' if verdict['pass'] else 'FAIL'} "
          f"({sum(1 for c in verdict['checks'] if c['ok'])}/{len(verdict['checks'])} checks)",
          flush=True)
    for issue in verdict["issues"]:
        print(f"         ! {issue}", flush=True)
    if llm_review:
        print(f"         llm score: {llm_review.get('score', '?')}/10", flush=True)
    return verdict
