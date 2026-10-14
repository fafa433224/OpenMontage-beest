"""Code2motion — LLM-authored bespoke motion code, safely.

Why this module exists
----------------------
The templated path (fixed Remotion components + parameterized HyperFrames
cards) is safe but ceiling-limited: every video is a re-arrangement of the
same vocabulary. The atelier path (project-local authored code) is fully
creative but needs a *generator* plus *guardrails*. This module is that
generator + guardrail stack, following the repo doctrine:

    the LLM only shifts choices — the seeded director and the validation
    ladder guarantee a valid render.

Pipeline
--------
``generate_hyperframes_card()`` asks the runtime LLM (``lib/llm_client``)
to author a complete HyperFrames ``index.html`` for an intro/outro card,
then climbs a validation ladder:

1. **Structural lint** (``lint_hyperframes_html``) — the HyperFrames
   contract: single root ``data-composition-id``, clip elements with
   ``data-start``/``data-duration``/``data-track-index``, leaf audio
   clips with ids, GSAP timeline paused + registered at
   ``window.__timelines["root"]``, no ``gsap.utils.random``, no
   unbounded repeats, no layout-property animation, no remote assets.
2. **Repair loop** — lint failures are fed back to the LLM (max
   ``max_repairs`` rounds, default 2) with the exact violations.
3. **Render probe** — the caller renders with ``npx hyperframes render``
   and checks the output duration with ffprobe (the Kaggle teardown-hang
   guard already in ``render_voiceover_video.py``).
4. **Fallback** — on any failure the parameterized brand template
   (``scripts/kaggle/hyperframes/{intro,outro}/index.html``) is used;
   the run never fails because of code2motion. Every decision is
   returned as a provenance record for the decision log.

Fable-derived authoring rules baked into the prompt:
* **fractional internal timing** — every GSAP position/duration is a
  fraction of the card duration (tempo-portable choreography);
* **continuity anchors** — the intro ends on the visual state the body
  opens with (bg color family, accent, motion direction), the outro
  opens where the body closes;
* **determinism** — fixed value arrays instead of random.

Public surface
--------------
    from lib.code2motion import (
        fill_hyperframes_template, lint_hyperframes_html,
        generate_hyperframes_card, code2motion_status,
    )
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

# Reference durations the templates' choreography is authored against.
INTRO_REFERENCE_S = 5.4
OUTRO_REFERENCE_S = 4.8

REPO_ROOT = Path(__file__).resolve().parent.parent
HYPERFRAMES_TEMPLATES = REPO_ROOT / "scripts" / "kaggle" / "hyperframes"

# ---------------------------------------------------------------------------
# Token filling — brand identity + fractional timing tokens
# ---------------------------------------------------------------------------


def _rgb_triplet(hex_color: str) -> str:
    clean = hex_color.lstrip("#")
    if len(clean) == 3:
        clean = "".join(ch * 2 for ch in clean)
    if len(clean) != 6:
        return "11, 16, 38"
    r, g, b = int(clean[0:2], 16), int(clean[2:4], 16), int(clean[4:6], 16)
    return f"{r}, {g}, {b}"


def fill_hyperframes_template(html: str, brand: Any, kind: str) -> str:
    """Fill ``{{TOKENS}}`` in a HyperFrames intro/outro template.

    Identity tokens come from the brand profile; timing tokens (card
    duration + SFX cue positions) are computed from the brand's duration
    so the fractional-timing template renders correctly at any duration.
    ``kind`` is ``"intro"`` or ``"outro"``.
    """
    if kind not in ("intro", "outro"):
        raise ValueError(f"kind must be intro|outro, got {kind!r}")

    intro = brand.intro_config
    outro = brand.outro_config
    reference = INTRO_REFERENCE_S if kind == "intro" else OUTRO_REFERENCE_S
    duration = float(
        (intro if kind == "intro" else outro).get("duration_s") or reference
    )
    scale = duration / reference

    tokens = {
        # identity
        "WORDMARK": (intro if kind == "intro" else outro).get("wordmark")
        or brand.name,
        "KICKER": intro.get("kicker") or "",
        "TAGLINE": intro.get("tagline") or brand.tagline,
        "CTA": outro.get("cta") or "Built with the open pipeline",
        "URL": outro.get("url") or brand.url,
        # palette
        "BG": brand.bg,
        "FG": brand.fg,
        "PRIMARY": brand.primary,
        "ACCENT": brand.accent,
        "BG_RGB": _rgb_triplet(brand.bg),
        "FG_RGB": _rgb_triplet(brand.fg),
        "PRIMARY_RGB": _rgb_triplet(brand.primary),
        "ACCENT_RGB": _rgb_triplet(brand.accent),
        # typography
        "FONT_HEAD": brand.fonts["heading"],
        "FONT_BODY": brand.fonts["body"],
        # timing
        "DURATION": f"{duration:g}",
        "S_RISER": f"{0.15 * scale:.2f}",
        "S_IMPACT": f"{1.35 * scale:.2f}",
        "S_WHOOSH": f"{(4.45 if kind == 'intro' else 3.7) * scale:.2f}",
        "S_CHIME": f"{0.35 * scale:.2f}",
    }
    out = html
    for token, value in tokens.items():
        out = out.replace("{{" + token + "}}", str(value))
    return out


# ---------------------------------------------------------------------------
# Structural lint — the HyperFrames authoring contract
# ---------------------------------------------------------------------------

_LINT_RULES: list[tuple[str, str, re.Pattern]] = [
    (
        "root_composition",
        'exactly one element with data-composition-id="root"',
        re.compile(r'data-composition-id="root"'),
    ),
    (
        "timeline_registered",
        'paused GSAP timeline registered at window.__timelines["root"]',
        re.compile(
            r'gsap\.timeline\(\s*\{\s*paused:\s*true\s*\}\s*\).*'
            r'__timelines\[\s*["\']root["\']\s*\]\s*=\s*tl',
            re.S,
        ),
    ),
    (
        "gsap_local",
        "GSAP loaded from the local assets/gsap.min.js (no CDN)",
        re.compile(r'<script[^>]+src="assets/gsap\.min\.js"'),
    ),
    (
        "visual_clip",
        'one visual clip with class="clip", data-start, data-duration, data-track-index',
        re.compile(
            r'class="clip"[^>]*data-start="[\d.]+"[^>]*data-duration="[\d.]+"[^>]*data-track-index'
        ),
    ),
]


def lint_hyperframes_html(html: str) -> list[str]:
    """Return a list of contract violations (empty list = clean).

    Checks the HyperFrames rendering contract plus the code2motion
    safety rules: deterministic values, bounded repeats, transform-only
    animation, no remote assets, no dangerous browser APIs. HTML
    comments are stripped first — docs legitimately mention forbidden
    APIs, only actual code is judged.
    """
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    violations: list[str] = []

    for rule_id, description, pattern in _LINT_RULES:
        if not pattern.search(html):
            violations.append(f"{rule_id}: {description}")

    # Multiple root ELEMENTS would confuse the engine (CSS selectors in
    # <style> legitimately re-mention the attribute — count divs only).
    if len(re.findall(r"<div[^>]*data-composition-id=", html)) != 1:
        violations.append("root_composition: exactly one root <div data-composition-id> element is required")

    # Determinism: random values break parallel-frame rendering.
    # (Require call syntax — comments/docs legitimately mention the APIs.)
    if re.search(r"gsap\.utils\.random\s*\(", html):
        violations.append(
            "determinism: gsap.utils.random() is forbidden — use fixed value "
            "arrays so parallel workers render identical frames"
        )
    if re.search(r"Math\.random\s*\(", html):
        violations.append("determinism: Math.random() is forbidden in timelines")

    # Unbounded repeats never finish under seek-by-frame capture.
    for m in re.finditer(r"repeat:\s*(-1|Infinity)", html):
        violations.append(
            f"bounded_repeats: unbounded repeat at ...{html[max(0, m.start() - 60):m.start() + 20]!r}"
        )

    # Layout props animate on the main thread and snap — transforms only.
    if re.search(r"letterSpacing\s*:", html) and re.search(
        r"(fromTo|to)\(", html
    ):
        # letterSpacing is allowed in static CSS but never as a GSAP target.
        gsap_blocks = re.findall(
            r"tl\.(?:fromTo|to)\(\s*[^)]*", html, re.S
        )
        for block in gsap_blocks:
            if "letterSpacing" in block or re.search(r"\b(top|left)\s*:", block):
                violations.append(
                    "transform_only: GSAP must animate transform/opacity only "
                    "(found layout property in a tween)"
                )
                break

    # Remote assets break offline Kaggle renders.
    for m in re.finditer(r'(?:src|href)=["\'](https?:)?//[^"\']+', html):
        if "fonts.googleapis" in m.group(0) or "fonts.gstatic" in m.group(0):
            continue  # font CDNs fail soft in headless capture
        violations.append(
            f"offline_assets: remote asset {m.group(0)[:80]} — bundle assets locally"
        )

    # Escape hatches a generated card must never use.
    for pattern, why in [
        (r"document\.write", "document.write is forbidden"),
        (r"\beval\s*\(", "eval is forbidden"),
        (r"new\s+Function", "new Function is forbidden"),
        (r"fetch\s*\(", "fetch is forbidden — assets are staged on disk"),
        (r"XMLHttpRequest", "XMLHttpRequest is forbidden"),
        (r"import\s*\(", "dynamic import is forbidden"),
        (r"<iframe", "iframes are forbidden"),
    ]:
        if re.search(pattern, html):
            violations.append(f"forbidden_api: {why}")

    # SFX audio leaves need stable ids for media discovery.
    if "<audio" in html and not re.search(r'<audio[^>]+id="', html):
        violations.append("audio_ids: <audio> leaf clips must carry id attributes")

    return violations


# ---------------------------------------------------------------------------
# Generation — the LLM authors the card, the ladder keeps it safe
# ---------------------------------------------------------------------------

CARD_SYSTEM_PROMPT = """You are a senior motion designer who writes HyperFrames
composition code (HTML + CSS + GSAP) rendered by a seek-by-frame headless
browser at 1920x1080@30fps. You write ONE complete index.html file.

HARD CONTRACT (violations are rejected and you must fix them):
1. Exactly one root element: <div data-composition-id="root" data-start="0"
   data-duration="{duration}" data-width="1920" data-height="1080" data-fps="30">
2. Inside it: exactly one visual clip
   <div id="scene" class="clip" data-start="0" data-duration="{duration}"
   data-track-index="0"> holding the entire scene.
3. Optional SFX: leaf <audio id="sfx-..." data-start="..." data-duration="..."
   data-track-index="3" src="assets/WHOOSH.wav" data-volume="0.4"></audio>
   — only riser.wav, impact.wav, whoosh.wav, chime.wav exist in assets/.
4. Load GSAP locally: <script src="assets/gsap.min.js"></script> — never a CDN.
5. Build the timeline as:
     const tl = gsap.timeline({{ paused: true }});
     ... your tweens ...
     window.__timelines = window.__timelines || {{}};
     window.__timelines["root"] = tl;
6. GSAP animates transform and opacity ONLY — never top/left/width/height/
   letterSpacing/fontSize (they snap under frame capture).
7. Determinism: NO gsap.utils.random, NO Math.random. Use fixed arrays for
   per-element offsets.
8. Every repeat is bounded (repeat: 1..4); never -1 or Infinity.
9. No fetch/eval/new Function/document.write/iframes/remote assets.
10. FRACTIONAL TIMING: author every tween position and duration as a
    fraction of the card duration D ({duration}s). Define
    `const D = {duration};` and place beats as `0.18*D`, `0.62*D` etc, so
    the choreography survives duration changes.
11. CONTINUITY: the card must end (intro) / begin (outro) on the brand's
    background color and accent so the cut into the Remotion body feels
    like one film — the last visible frame should be close to the body's
    opening palette ({bg} base, {accent} accent).
12. Typography: use font-family '{font_head}', 'Outfit', 'Segoe UI', Arial,
    sans-serif. No external font loading.
13. Creativity is the point: compose a bespoke choreography a human motion
    designer would sign — layered depth, staggered cascades, masked wipes,
    choreographed camera, counter-motion, a signature moment. Never a
    static card with one fade.

Answer with ONLY the complete HTML file content, no markdown fences, no
explanations."""


def _card_user_prompt(kind: str, brand: Any, script: dict[str, Any]) -> str:
    secs = script.get("sections") or []
    preview = " | ".join(
        f"{s.get('heading', '')}: {(s.get('text') or '')[:80]}" for s in secs[:3]
    )
    enabled = [
        f"{name}.wav"
        for name, on in (
            ("riser", brand.sfx.get("riser", True)),
            ("impact", brand.sfx.get("impact", True)),
            ("whoosh", brand.sfx.get("whoosh", True)),
            ("chime", brand.sfx.get("chime", True)),
        )
        if on
    ]
    sfx_line = (
        f"SFX available in assets/ (use only these, each at most once): "
        f"{', '.join(enabled) if enabled else 'none — author a silent card'}"
    )
    if kind == "intro":
        job = (
            f"Author the INTRO card ({brand.intro_config.get('duration_s', 5.4)}s) "
            f"for the channel '{brand.name}'. Wordmark: "
            f"{brand.intro_config.get('wordmark') or brand.name}. Kicker: "
            f"{brand.intro_config.get('kicker') or '(none)'}. Tagline: "
            f"{brand.intro_config.get('tagline') or brand.tagline}. The video "
            f"topic: {script.get('topic', '')}. Script opening: {preview}. "
            "Hook the viewer: the intro must make them want the first line."
        )
    else:
        job = (
            f"Author the OUTRO card ({brand.outro_config.get('duration_s', 4.8)}s) "
            f"for the channel '{brand.name}'. Wordmark: "
            f"{brand.outro_config.get('wordmark') or brand.name}. CTA: "
            f"{brand.outro_config.get('cta') or '(subscribe)'}. URL: "
            f"{brand.outro_config.get('url') or brand.url}. "
            "Close the film gracefully and leave the CTA on screen."
        )
    return (
        f"{job}\n{sfx_line}\nBrand palette: bg {brand.bg}, fg {brand.fg}, primary "
        f"{brand.primary}, accent {brand.accent}. Motion signature: "
        f"{brand.timing_curve} timing, pacing '{brand.pacing}', camera drift "
        f"{brand.camera_drift}. Allowed bg styles vocabulary (for inspiration, "
        f"you may deviate): {brand.bg_pool}."
    )


def _extract_html(raw: str) -> str:
    """Strip markdown fences / prose around the HTML the LLM returns."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
        return text.strip()
    # Tolerate a leading sentence: cut everything before <!DOCTYPE or <html.
    m = re.search(r"(<!DOCTYPE|<html)", text, re.I)
    if m and m.start() > 0:
        text = text[m.start():]
    return text


def generate_hyperframes_card(
    kind: str,
    brand: Any,
    script: dict[str, Any],
    llm_client: Any,
    max_repairs: int = 2,
) -> dict[str, Any]:
    """Ask the LLM to author a bespoke intro/outro card.

    Returns ``{"html": str|null, "provenance": {...}, "violations": [...]}``.
    ``html`` is None when generation failed (caller falls back to the
    brand template — code2motion must never fail a render).
    """
    reference = INTRO_REFERENCE_S if kind == "intro" else OUTRO_REFERENCE_S
    duration = float(
        (brand.intro_config if kind == "intro" else brand.outro_config).get(
            "duration_s"
        )
        or reference
    )
    provenance: dict[str, Any] = {
        "kind": kind,
        "mode": "code2motion",
        "channel": brand.channel_id,
        "duration_s": duration,
        "model": getattr(llm_client, "model", "unknown"),
        "attempts": 0,
    }

    if llm_client is None:
        provenance["reason"] = "no llm client"
        return {"html": None, "provenance": provenance, "violations": ["no llm client"]}

    system = CARD_SYSTEM_PROMPT.format(
        duration=f"{duration:g}",
        bg=brand.bg,
        accent=brand.accent,
        font_head=brand.fonts["heading"],
    )
    messages = [{"role": "user", "content": _card_user_prompt(kind, brand, script)}]
    html_text: Optional[str] = None
    violations: list[str] = []

    for attempt in range(1 + max(0, max_repairs)):
        provenance["attempts"] = attempt + 1
        try:
            raw = llm_client.chat(
                messages,
                system=system,
                temperature=0.9 if attempt == 0 else 0.4,
                max_tokens=6000,
            )
        except Exception as exc:  # noqa: BLE001 - LLM outage falls back
            provenance["reason"] = f"llm error: {exc}"
            return {"html": None, "provenance": provenance, "violations": [str(exc)]}

        html_text = _extract_html(str(raw))
        violations = lint_hyperframes_html(html_text)
        if not violations:
            provenance["status"] = "generated_clean"
            provenance["chars"] = len(html_text)
            return {"html": html_text, "provenance": provenance, "violations": []}

        # Feed the violations back for repair.
        messages.append(
            {
                "role": "assistant",
                "content": (html_text or "")[:8000],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": (
                    "Your HTML violated the contract — fix ALL of these and "
                    "return the complete corrected file:\n- "
                    + "\n- ".join(violations[:12])
                ),
            }
        )

    provenance["status"] = "fallback_after_repairs"
    provenance["violations"] = violations[:12]
    return {"html": None, "provenance": provenance, "violations": violations}


def code2motion_status(llm_client: Any = None) -> dict[str, Any]:
    """Cheap status probe: can code2motion run on this box?"""
    from lib.llm_client import llm_status

    status = llm_status()
    return {
        "llm_available": bool(status.get("available")),
        "llm_provider": status.get("provider"),
        "llm_model": status.get("model"),
        "hyperframes_npx": bool(shutil.which("npx")),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "templates": {
            k: (HYPERFRAMES_TEMPLATES / k / "index.html").exists()
            for k in ("intro", "outro")
        },
    }


def render_probe_duration(mp4_path: str | Path) -> Optional[float]:
    """ffprobe the rendered output — the render's only ground truth."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                str(mp4_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        import json

        return float(json.loads(result.stdout)["format"]["duration"])
    except Exception:
        return None
