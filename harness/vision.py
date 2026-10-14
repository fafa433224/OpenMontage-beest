"""Visual audit — the harness finally *looks* at what it rendered.

Why this module exists
----------------------
The Fable 5.1 write-ups all describe the same load-bearing mechanic: the
model renders, **inspects still frames and a motion preview**, revises the
scene, and only then commits to the final sequence. That inspect-and-revise
loop — not the model choice — is what produced a clean 37-second film where
the earlier one-shot attempt clipped a camera through a staircase.

Our harness did none of that. It rendered blind and then checked four
things with ffprobe: does the file exist, does it have streams, is the
duration sane, is the resolution right. Every creative failure mode — a
scene that never animates, a palette that never changes, a frame where the
headline sits on top of the artwork, twelve scenes that all look identical —
passed QC green.

This module is the missing layer, in two halves:

**Deterministic metrics** (always available, no LLM, no network — this is
what runs on a Kaggle session with no API key). Per scene and per video:

* ``motion_energy`` — mean absolute frame difference inside a scene. A
  scene below the floor is *dead*: the render succeeded and nothing moved.
  This is a direct measurement of "robotic".
* ``perceptual_variety`` — pairwise Hamming distance between per-scene
  dHash fingerprints. All scenes hashing alike means the video collapsed
  back into one template. This is a direct measurement of "every video /
  every scene looks the same", the exact complaint the repo set out to fix.
* ``palette_spread`` — hue distribution across scene midframes, so a
  channel-wide look can be verified without eyeballing.
* ``contrast_floor`` — luma separation between the bright (text) and dark
  (background) percentiles; catches unreadable type on a busy background.
* ``dead_frames`` / ``flash_frames`` — blank and blown-out frames.
* ``edge_bleed`` — text energy in the outer 4% margin, the cheap proxy for
  "the headline is running off the frame".

**Vision critique** (optional, when a model with vision is configured): the
sampled frames plus the plan are handed to the model, which returns a
structured per-scene critique. Advisory, never a gate — same doctrine as the
existing LLM review.

Public surface
--------------
    from harness.vision import visual_audit, audit_report_text

    report = visual_audit(video, scene_spans, out_dir, llm=client, script=script)
    report["pass"], report["issues"], report["metrics"], report["critique"]
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Thresholds — deliberately conservative. These fire on genuine defects, not
# on taste; anything subjective belongs to the vision critique, not here.
# ---------------------------------------------------------------------------

MOTION_ENERGY_FLOOR = 0.010     # mean abs frame diff (0..1 luma) inside a scene
VARIETY_MIN_DISTINCT = 0.55     # fraction of scene pairs that must differ
#: p99 - p05 luma. NOT p95 - p05: type covers well under 5% of a frame's
#: pixels, so p95 lands on the background glow and measures how much internal
#: spread the *plate* has, not whether the words are readable. Measured on
#: real frames of the same scene: a full-scale frame whose glyphs are
#: #F4F6FB on #09191B scores 0.533 on p99-p05 but only 0.258 on p95-p05,
#: while a genuinely washed frame scores 0.165 and 0.143 respectively. p95
#: cannot tell those apart; p99 can. Floor calibrated between them.
CONTRAST_FLOOR = 0.30           # p99 - p05 luma
DEAD_FRAME_LUMA = 0.012         # essentially black
FLASH_FRAME_LUMA = 0.985        # essentially white
EDGE_BLEED_MAX = 0.85           # text energy fraction allowed in outer margin
MAX_SCENES_FOR_FULL_SAMPLE = 48

VISION_SYSTEM_PROMPT = """You are the visual director of a video production
pipeline doing post-render review. You are shown sampled frames from a
rendered video, in order, with their scene numbers and timestamps.

Judge what a viewer would feel, not what a spec says:
- Does the composition read? Is the type legible against what is behind it?
- Do consecutive scenes feel like one film (shared palette/motion
  language) or like unrelated slides?
- Is there variety in STRUCTURE between scenes, or is every scene the same
  layout wearing different words? Repetition is the failure we care most
  about.
- Does any frame look broken: clipped type, a stray element, an empty
  frame, a flat/unfinished look, content colliding with the frame edge?
- Is the motion likely to read as intentional? You only see stills — infer
  from framing, but do not invent motion defects you cannot see.

Be specific and terse. Name the scene number for every issue. Never invent
an issue to seem thorough; an empty issues list is a valid answer.

Answer with ONLY a JSON object:
{
  "pass": true,
  "score": 0-10,
  "structure_variety": 0-10,
  "legibility": 0-10,
  "coherence": 0-10,
  "issues": [{"scene": 0, "severity": "high|medium|low", "problem": "...", "fix": "..."}],
  "wins": ["..."],
  "next_video_notes": ["..."]
}"""


# ---------------------------------------------------------------------------
# Frame sampling
# ---------------------------------------------------------------------------

def _ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def extract_frames(
    video: Path,
    times: list[float],
    out_dir: Path,
    *,
    width: int = 1280,
) -> list[Optional[Path]]:
    """Extract one frame per timestamp, in order.

    ``width`` is not a cosmetic choice. Legibility is measured on these
    frames, and a 46 px body line rendered at 1920 wide becomes 15 px at 640
    — thin, heavily antialiased, and dimmer at the 99th percentile than the
    pixels a viewer actually sees. Auditing a 640-wide copy reported
    contrast failures on scenes whose full-scale glyphs measure #F4F6FB on
    #09191B (16:1). 1280 keeps the measurement honest at ~2/3 scale; pass a
    smaller width only for a fast preview, and read the severity with that in
    mind.

    Uses one ffmpeg invocation per timestamp with an input seek, which is
    fast (keyframe seek) and — unlike a single ``select`` filter across the
    whole file — never silently drops a requested frame. Missing frames come
    back as ``None`` rather than shifting the list, so scene numbering stays
    correct even on a partially-decodable file.
    """
    ffmpeg = _ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)
    if not ffmpeg:
        return [None] * len(times)

    results: list[Optional[Path]] = []
    for i, t in enumerate(times):
        dest = out_dir / f"f{i:04d}.png"
        if dest.exists():
            results.append(dest)
            continue
        cmd = [
            ffmpeg, "-v", "error", "-y",
            "-ss", f"{max(0.0, float(t)):.3f}",
            "-i", str(video),
            "-frames:v", "1",
            # `min(iw,width)` — downscale for speed, but NEVER upscale. A
            # 320-wide source blown up to 1280 gains no detail and loses
            # signal: the smoothing makes adjacent frames hash more alike, so
            # a genuinely moving video measured less varied the larger it was
            # asked to be. Scaling up also inflated the reported sample width,
            # which is what the legibility verdict keys on.
            "-vf", f"scale=min(iw\\,{width}):-2",
            str(dest),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=60, check=False)
        except Exception:
            pass
        results.append(dest if dest.exists() else None)
    return results


# ---------------------------------------------------------------------------
# Deterministic image metrics (PIL + numpy; both already required)
# ---------------------------------------------------------------------------

def _load_luma(path: Path):
    """Return (luma ndarray float32 0..1, rgb ndarray). None on failure."""
    try:
        import numpy as np
        from PIL import Image

        with Image.open(path) as im:
            rgb = im.convert("RGB")
            arr = np.asarray(rgb).astype("float32") / 255.0
        luma = arr[:, :, 0] * 0.299 + arr[:, :, 1] * 0.587 + arr[:, :, 2] * 0.114
        return luma, arr
    except Exception:
        return None


def frame_metrics(path: Path) -> Optional[dict[str, float]]:
    """Per-frame deterministic metrics. ``None`` when the frame is unreadable."""
    loaded = _load_luma(path)
    if loaded is None:
        return None
    luma, rgb = loaded
    try:
        import numpy as np

        h, w = luma.shape
        p05, p50, p95, p99 = (float(np.percentile(luma, q)) for q in (5, 50, 95, 99))

        # Edge density: a cheap Sobel-free gradient magnitude. Text and UI
        # detail raise this; flat backgrounds do not.
        gx = np.abs(np.diff(luma, axis=1)).mean() if w > 1 else 0.0
        gy = np.abs(np.diff(luma, axis=0)).mean() if h > 1 else 0.0
        edge = float(gx + gy)

        # Edge bleed: detail energy in the outer 4% frame margin. Clipped
        # type shows up here before it shows up in any subjective review.
        mx, my = max(1, int(w * 0.04)), max(1, int(h * 0.04))
        border = np.concatenate([
            luma[:my, :].ravel(), luma[-my:, :].ravel(),
            luma[:, :mx].ravel(), luma[:, -mx:].ravel(),
        ])
        border_edge = float(np.abs(np.diff(border)).mean()) if border.size > 1 else 0.0

        # Colourfulness (Hasler-Süsstrunk style): how far the channels sit
        # from grey. Flat grey frames read as unfinished.
        rg = rgb[:, :, 0] - rgb[:, :, 1]
        yb = 0.5 * (rgb[:, :, 0] + rgb[:, :, 1]) - rgb[:, :, 2]
        colorfulness = float(math.sqrt(rg.std() ** 2 + yb.std() ** 2)
                             + 0.3 * math.sqrt(rg.mean() ** 2 + yb.mean() ** 2))

        return {
            "luma_mean": float(luma.mean()),
            "luma_std": float(luma.std()),
            "luma_p05": p05,
            "luma_p50": p50,
            "luma_p95": p95,
            "luma_p99": p99,
            "contrast": float(p99 - p05),
            "edge_density": edge,
            "edge_bleed": border_edge,
            "colorfulness": colorfulness,
        }
    except Exception:
        return None


_LUMA_GRID = 16
_EDGE_GRID = 8


def frame_vector(path: Path) -> Optional[dict[str, Any]]:
    """Compact signature used for variety + motion comparison."""
    loaded = _load_luma(path)
    if loaded is None:
        return None
    luma, _rgb = loaded
    try:
        import numpy as np
        from PIL import Image

        # dHash on a 9x8 grid — robust to codec noise, sensitive to layout.
        with Image.open(path) as im:
            small = im.convert("L").resize((9, 8), Image.LANCZOS)
            a = np.asarray(small).astype("int16")
        bits = (a[:, 1:] > a[:, :-1]).astype("uint8").ravel()
        dhash = int("".join(str(b) for b in bits), 2)

        h, w = luma.shape
        gh, gw = max(1, h // _LUMA_GRID), max(1, w // _LUMA_GRID)
        grid = luma[: gh * _LUMA_GRID, : gw * _LUMA_GRID].reshape(
            _LUMA_GRID, gh, _LUMA_GRID, gw
        ).mean(axis=(1, 3))

        eh, ew = max(1, h // _EDGE_GRID), max(1, w // _EDGE_GRID)
        tr = luma[: eh * _EDGE_GRID, : ew * _EDGE_GRID].reshape(
            _EDGE_GRID, eh, _EDGE_GRID, ew
        )
        edge_grid = np.abs(np.diff(tr, axis=1)).mean(axis=(1, 3))
        edge_grid = edge_grid[:, : _EDGE_GRID]

        return {
            "dhash": dhash,
            "grid": grid.astype("float32"),
            "edge_grid": edge_grid.astype("float32"),
        }
    except Exception:
        return None


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _grid_delta(a, b) -> float:
    try:
        import numpy as np

        return float(np.abs(a - b).mean())
    except Exception:
        return 0.0


def _edge_delta(a, b) -> float:
    try:
        import numpy as np

        if a.shape != b.shape:
            return 0.0
        return float(np.abs(a - b).mean())
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------

def _default_scene_spans(duration: float, count: int = 8) -> list[dict[str, Any]]:
    """Evenly divide the video when the caller has no scene map."""
    if duration <= 0 or count <= 0:
        return []
    step = duration / count
    return [
        {"index": i, "start": round(i * step, 3), "end": round((i + 1) * step, 3)}
        for i in range(count)
    ]


def scene_spans_from_timing(timing: dict[str, Any], duration: float) -> list[dict[str, Any]]:
    """Build scene spans from a render timing artifact when available."""
    spans: list[dict[str, Any]] = []
    intro = float(timing.get("hyperframes_intro_s") or 0.0)
    outro = float(timing.get("hyperframes_outro_s") or 0.0)
    cuts = timing.get("cuts") or timing.get("scenes") or []
    if isinstance(cuts, list) and cuts:
        for i, c in enumerate(cuts):
            if not isinstance(c, dict):
                continue
            start = c.get("start") if "start" in c else c.get("in_seconds")
            end = c.get("end") if "end" in c else c.get("out_seconds")
            if start is None or end is None:
                continue
            spans.append({
                "index": i,
                "start": float(start) + intro,
                "end": float(end) + intro,
                "kind": "scene",
            })
        return spans
    body = max(0.0, duration - intro - outro)
    if body > 0:
        if intro > 0:
            spans.append({"index": -1, "start": 0.0, "end": intro, "kind": "intro"})
        spans.extend([
            {**s, "start": s["start"] + intro, "end": s["end"] + intro, "kind": "scene"}
            for s in _default_scene_spans(body)
        ])
        if outro > 0:
            spans.append({"index": -2, "start": duration - outro, "end": duration, "kind": "outro"})
    return spans


def visual_audit(
    video: Path,
    out_dir: Path,
    *,
    scene_spans: Optional[list[dict[str, Any]]] = None,
    llm: Any = None,
    script: Optional[dict[str, Any]] = None,
    samples_per_scene: int = 3,
    frames_per_scene_for_llm: int = 1,
    max_llm_frames: int = 12,
    keep_frames: bool = True,
    frame_width: int = 1280,
) -> dict[str, Any]:
    """Look at a rendered video and say what is actually wrong with it.

    Deterministic metrics always run. The vision critique runs only when an
    ``llm`` with vision is supplied — and is advisory, so a model outage can
    never block delivery.
    """
    video = Path(video)
    report: dict[str, Any] = {
        "video": str(video),
        "pass": True,
        "issues": [],
        "metrics": {},
        "scenes": [],
        "critique": None,
        "frames_dir": None,
    }
    if not video.exists():
        report["pass"] = False
        report["issues"].append({"severity": "high", "problem": "video file missing",
                                 "fix": "check the render stage"})
        return report

    duration = _probe_duration(video) or 0.0
    spans = scene_spans or _default_scene_spans(duration)
    if len(spans) > MAX_SCENES_FOR_FULL_SAMPLE:
        spans = spans[:MAX_SCENES_FOR_FULL_SAMPLE]
    if not spans:
        return report

    frames_root = out_dir / "qc-frames"
    #: Below this, legibility findings are advisory: the frames are too
    #: downscaled for thin type to survive intact, so a "low contrast" reading
    #: says more about the resampling than about the video. Keyed on the
    #: width actually sampled — min(source, requested) — not the requested
    #: one, or a small source would be judged as if it had been captured at
    #: full resolution.
    source_width = _probe_width(video)
    sampled_width = min(source_width, frame_width) if source_width else frame_width
    legibility_reliable = sampled_width >= 1280
    if frames_root.exists() and not keep_frames:
        shutil.rmtree(frames_root, ignore_errors=True)
    frames_root.mkdir(parents=True, exist_ok=True)

    # ---- sample ---------------------------------------------------------
    scene_samples: list[dict[str, Any]] = []
    for span in spans:
        start, end = float(span["start"]), float(span["end"])
        length = max(0.05, end - start)
        idx = span.get("index", 0)
        if samples_per_scene <= 1:
            offsets = [0.5]
        else:
            # Never sample the exact edges: scene transitions live there and
            # a transition frame is not representative of the scene.
            offsets = [(i + 0.5) / samples_per_scene for i in range(samples_per_scene)]
            offsets = [min(0.88, max(0.12, o)) for o in offsets]
        times = [start + length * o for o in offsets]
        paths = extract_frames(video, times, frames_root / f"scene{idx:03d}",
                               width=frame_width)
        scene_samples.append({"span": span, "times": times, "paths": paths})

    # ---- per-scene metrics ----------------------------------------------
    issues: list[dict[str, Any]] = []
    scene_reports: list[dict[str, Any]] = []
    vectors: list[dict[str, Any]] = []
    all_luma: list[float] = []
    dead = 0
    flash = 0

    for entry in scene_samples:
        span = entry["span"]
        idx = span.get("index", 0)
        metrics: list[dict[str, float]] = []
        vecs: list[dict[str, Any]] = []
        for p in entry["paths"]:
            if p is None:
                continue
            m = frame_metrics(p)
            if m:
                metrics.append(m)
                all_luma.append(m["luma_mean"])
                if m["luma_mean"] < DEAD_FRAME_LUMA:
                    dead += 1
                if m["luma_mean"] > FLASH_FRAME_LUMA:
                    flash += 1
            v = frame_vector(p)
            if v:
                vecs.append(v)

        if not metrics:
            scene_reports.append({"index": idx, "start": span["start"], "end": span["end"],
                                  "sampled": 0, "error": "no readable frames"})
            issues.append({
                "severity": "high", "scene": idx,
                "problem": f"scene {idx}: no decodable frames at {span['start']:.1f}-{span['end']:.1f}s",
                "fix": "check the render for a corrupt or zero-length segment",
            })
            continue

        def avg(key: str) -> float:
            return sum(m[key] for m in metrics) / len(metrics)

        # Motion: mean difference between consecutive samples inside the scene.
        motion = 0.0
        if len(vecs) >= 2:
            deltas = [_grid_delta(vecs[i]["grid"], vecs[i + 1]["grid"])
                      for i in range(len(vecs) - 1)]
            motion = sum(deltas) / len(deltas)
        elif len(vecs) == 1:
            # Single sample cannot measure intra-scene motion; fall back to
            # the difference against the previous scene's sample so a wholly
            # static timeline is still detectable.
            if vectors:
                motion = _grid_delta(vectors[-1]["grid"], vecs[0]["grid"])
            else:
                motion = float("nan")

        sr = {
            "index": idx,
            "start": round(span["start"], 2),
            "end": round(span["end"], 2),
            "kind": span.get("kind", "scene"),
            "sampled": len(metrics),
            "luma_mean": round(avg("luma_mean"), 4),
            "contrast": round(avg("contrast"), 4),
            "edge_density": round(avg("edge_density"), 4),
            "edge_bleed": round(avg("edge_bleed"), 4),
            "colorfulness": round(avg("colorfulness"), 4),
            "motion_energy": None if math.isnan(motion) else round(motion, 5),
        }
        scene_reports.append(sr)
        vectors.extend(vecs)

        if isinstance(motion, float) and not math.isnan(motion) and motion < MOTION_ENERGY_FLOOR:
            issues.append({
                "severity": "medium", "scene": idx,
                "problem": f"scene {idx} is visually static "
                           f"(motion energy {motion:.4f} < {MOTION_ENERGY_FLOOR})",
                "fix": "the choreography did not animate; check that the scene "
                       "got a compiled spec and that layer enterAt/enterDur windows "
                       "are inside the scene duration",
            })
        if sr["contrast"] < CONTRAST_FLOOR:
            # A downscaled sample cannot settle a legibility question, so the
            # finding is kept but labelled for what it is instead of being
            # reported as a delivery defect.
            detail = "" if legibility_reliable else (
                f" — advisory only: sampled at {sampled_width}px wide, thin type "
                f"does not survive downscaling; re-audit the full-scale render"
            )
            issues.append({
                "severity": "medium" if legibility_reliable else "info",
                "scene": idx,
                "problem": f"scene {idx} has low contrast (p99-p05 luma "
                           f"{sr['contrast']:.3f} < {CONTRAST_FLOOR}){detail}",
                "fix": "type and background are too close in luminance; "
                       "raise the palette's text/bg separation",
            })
        if sr["edge_bleed"] > EDGE_BLEED_MAX * sr["edge_density"] and sr["edge_bleed"] > 0.06:
            issues.append({
                "severity": "low", "scene": idx,
                "problem": f"scene {idx} has heavy detail in the outer frame margin "
                           f"(edge bleed {sr['edge_bleed']:.3f})",
                "fix": "content may be running off the frame; check safe margins",
            })

    # ---- video-level variety -------------------------------------------
    distinct_hashes = {v["dhash"] for v in vectors}
    pairs = 0
    differing = 0
    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            pairs += 1
            if hamming(vectors[i]["dhash"], vectors[j]["dhash"]) >= 12:
                differing += 1
    variety_ratio = (differing / pairs) if pairs else 1.0

    structure_variety = 0.0
    if len(vectors) >= 2:
        edge_deltas = [
            _edge_delta(vectors[i]["edge_grid"], vectors[j]["edge_grid"])
            for i in range(len(vectors))
            for j in range(i + 1, min(len(vectors), i + 9))
            if vectors[i]["edge_grid"].shape == vectors[j]["edge_grid"].shape
        ]
        if edge_deltas:
            structure_variety = sum(edge_deltas) / len(edge_deltas)

    report["metrics"] = {
        "duration_s": round(duration, 2),
        "scenes_audited": len(scene_reports),
        "frames_sampled": sum(s["sampled"] for s in scene_reports),
        # Sample resolution: legibility findings are only conclusive at
        # >= 1280px wide (see extract_frames). This is the width actually
        # read, after capping at the source's own resolution.
        "frame_width": sampled_width,
        "requested_frame_width": frame_width,
        "distinct_frame_hashes": len(distinct_hashes),
        "variety_ratio": round(variety_ratio, 4),
        "structure_variety": round(structure_variety, 5),
        "motion_energy_mean": round(
            sum(s["motion_energy"] for s in scene_reports if s["motion_energy"] is not None)
            / max(1, len([s for s in scene_reports if s["motion_energy"] is not None])), 5
        ),
        "contrast_min": round(min((s["contrast"] for s in scene_reports), default=0.0), 4),
        "luma_mean": round(sum(all_luma) / len(all_luma), 4) if all_luma else 0.0,
        "dead_frames": dead,
        "flash_frames": flash,
    }
    report["scenes"] = scene_reports

    if variety_ratio < VARIETY_MIN_DISTINCT and len(vectors) >= 4:
        issues.append({
            "severity": "high", "scene": None,
            "problem": f"scenes look alike: only {variety_ratio:.0%} of sampled frame "
                       f"pairs differ (need {VARIETY_MIN_DISTINCT:.0%})",
            "fix": "output has collapsed back into one template — check that "
                   "compile_video produced distinct schemes and that the props "
                   "carry `bespoke` specs",
        })
    if dead:
        issues.append({"severity": "high", "scene": None,
                       "problem": f"{dead} near-black frames sampled",
                       "fix": "a segment rendered empty; check asset paths and b-roll"})
    if flash:
        issues.append({"severity": "medium", "scene": None,
                       "problem": f"{flash} blown-out white frames sampled",
                       "fix": "an overlay is saturating; check scrims and flares"})

    # ---- vision critique (advisory) -------------------------------------
    keep = [p for entry in scene_samples for p in entry["paths"] if p is not None]
    report["frames_dir"] = str(frames_root)
    if llm is not None and keep:
        report["critique"] = _vision_critique(
            keep, spans, llm, script, frames_per_scene_for_llm, max_llm_frames
        )
        if report["critique"]:
            for item in (report["critique"].get("issues") or []):
                if isinstance(item, dict):
                    issues.append({
                        "severity": str(item.get("severity") or "low"),
                        "scene": item.get("scene"),
                        "problem": f"[vision] {item.get('problem', '')}".strip(),
                        "fix": str(item.get("fix") or ""),
                    })

    report["issues"] = issues
    report["pass"] = not any(i.get("severity") in ("high", "medium") for i in issues)

    try:
        (out_dir / "visual-audit.json").write_text(json.dumps(report, indent=1))
    except Exception:
        pass
    return report


def _vision_critique(
    frames: list[Path],
    spans: list[dict[str, Any]],
    llm: Any,
    script: Optional[dict[str, Any]],
    per_scene: int,
    max_frames: int,
) -> Optional[dict[str, Any]]:
    """Ask a vision model to review sampled frames. Returns None on any failure."""
    # Cap the payload: a vision call with 40 full-res frames is slow, costly
    # and usually less useful than 12 well-chosen ones.
    stride = max(1, len(frames) // max_frames)
    chosen = frames[::stride][:max_frames]
    if not chosen:
        return None

    labels = []
    per = max(1, max(1, len(frames) // max(1, len(spans))))
    for i, path in enumerate(chosen):
        source = frames.index(path) if path in frames else i
        scene_no = source // per
        labels.append(f"frame {i}: scene ~{scene_no}  ({path.name})")

    plan = ""
    if script:
        secs = script.get("sections") or []
        plan = "PLAN — title: {}\n".format(script.get("title", "(untitled)"))
        plan += "\n".join(
            f"  section {i}: {s.get('heading', '')} — {(s.get('text') or '')[:120]}"
            for i, s in enumerate(secs[:14])
        )

    prompt = (
        f"{plan}\n\n"
        f"You are looking at {len(chosen)} frames sampled in order from the "
        f"rendered video. Frame order:\n" + "\n".join(labels) +
        "\n\nReview the visual result and answer with the JSON object specified."
    )
    try:
        out = llm.chat_vision_json(
            prompt, chosen, system=VISION_SYSTEM_PROMPT,
            temperature=0.2, max_tokens=1600, detail="low",
        )
        return out if isinstance(out, dict) else None
    except Exception:
        return None


def _probe_duration(video: Path) -> Optional[float]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", str(video)],
            capture_output=True, text=True, timeout=30,
        ).stdout
        return float(json.loads(out)["format"]["duration"])
    except Exception:
        return None


def _probe_width(video: Path) -> Optional[int]:
    """Width of the video's first stream, or None if ffprobe cannot say."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v",
             "-show_entries", "stream=width", "-of", "default=nw=1:nk=1", str(video)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip().splitlines()
        return int(out[0]) if out and out[0].isdigit() else None
    except Exception:
        return None


def audit_report_text(report: dict[str, Any]) -> str:
    """Human-readable one-screen summary for logs and the harness review."""
    m = report.get("metrics") or {}
    lines = [
        f"visual audit: {'PASS' if report.get('pass') else 'FAIL'} "
        f"({m.get('frames_sampled', 0)} frames over {m.get('scenes_audited', 0)} scenes)",
        f"  variety      {m.get('variety_ratio', 0):.0%} of frame pairs differ "
        f"({m.get('distinct_frame_hashes', 0)} distinct hashes)",
        f"  structure    {m.get('structure_variety', 0):.4f}",
        f"  motion       {m.get('motion_energy_mean', 0):.4f} mean energy",
        f"  contrast min {m.get('contrast_min', 0):.3f}",
        f"  dead/flash   {m.get('dead_frames', 0)} / {m.get('flash_frames', 0)}",
    ]
    crit = report.get("critique")
    if isinstance(crit, dict):
        lines.append(
            f"  vision       score {crit.get('score', '?')}/10, "
            f"variety {crit.get('structure_variety', '?')}/10, "
            f"legibility {crit.get('legibility', '?')}/10"
        )
    for issue in (report.get("issues") or [])[:12]:
        scene = issue.get("scene")
        where = f"scene {scene}: " if scene is not None else ""
        lines.append(f"  ! [{issue.get('severity')}] {where}{issue.get('problem')}")
    return "\n".join(lines)
