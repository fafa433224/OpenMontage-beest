"""Motion compiler — body scenes as authored motion, not template picks.

The problem this solves
-----------------------
v1/v2 motion design walked a fixed vocabulary: ``cut.type = "text_card"``
picked the TextCard component and the director chose its transition, colour
and alignment. Every dimension varied, but the *structure* never did — a
text card is always a text card, entering one of eight ways. That is why
the output reads as "smooth but robotic": the choreography is a lookup, and
the lookup runs out.

Code2motion fixed this for the two intro/outro cards. This module fixes it
for **the body of the video** — the 90% that carries the film.

The idea
--------
Stop *selecting* a component. *Compile* a choreography program.

A section (heading, body, mood, palette, brand) is compiled into a
:class:`ChoreographySpec` — a declarative, renderable description of a
bespoke scene: an ordered depth stack of layers, each carrying its own
motion verb, entry/exit window, stagger offset, easing and camera coupling;
plus a camera path, a texture budget and (usually) a signature moment.

The spec is produced by composing a **scheme** (a structural idea, e.g.
``kinetic-split``, ``depth-pan``, ``monolith``) with seeded choices from the
scheme's pools. Structural space is the point:

    12 schemes x ~6 typographic treatments x ~20 motion verbs per layer
      x 2-6 layers x camera paths x signature moments  >>  any fixed component set

Two videos on the same channel share the brand envelope (palette, fonts,
pacing, timing curve) and can still be structurally unrelated. That is the
brief: *channel A feels like channel A, channel B feels different, and no
two videos are the same film.*

Contract with the renderer
--------------------------
The spec is interpreted by ``remotion-composer/src/motion/BespokeScene.tsx``.
Both sides are versioned by ``SPEC_VERSION``; :func:`lint_spec` enforces the
contract before anything is rendered (the same validation-ladder discipline
code2motion uses). A spec that lints clean renders; a spec that does not is
never handed to Remotion.

Fable-derived rules baked in
----------------------------
* **Fractional internal timing** — every window is a fraction of scene
  duration (``enterAt=0.18`` means 18% into the scene), so pacing survives
  a duration change without re-authoring.
* **Continuity anchors** — :func:`compile_scene` receives the previous
  scene's closing state and biases the opening motion direction/palette
  toward it, so cuts feel like one film rather than a reel of cards.
* **Determinism** — all randomness is ``random.Random(seed)``; no
  ``Math.random`` ever reaches the renderer, so parallel frame capture is
  identical across workers.

Public surface
--------------
    from lib.motion_compiler import compile_scene, compile_video, lint_spec

    spec = compile_scene(section, brand, project_id, index=0, duration=8.0)
    lint_spec(spec)            # -> [] when clean
    plan = compile_video(script, brand, project_id)   # whole-video plan
"""
from __future__ import annotations

import hashlib
import random
from typing import Any, Optional

SPEC_VERSION = 1

# ---------------------------------------------------------------------------
# Vocabularies — the material the compiler composes with
# ---------------------------------------------------------------------------

#: Structural ideas. Each is a *different way of building a scene*, not a
#: different parameter set. This is the axis that was missing.
SCHEMES: tuple[str, ...] = (
    "stacked-editorial",
    "monolith",
    "kinetic-split",
    "depth-pan",
    "grid-cascade",
    "mask-parade",
    "orbital-hero",
    "focus-pull",
    "ribbon-weave",
    "data-reveal",
    "curtain-call",
    "type-storm",
)

#: Motion verbs the renderer implements. Each maps to a transform track.
MOTION_VERBS: tuple[str, ...] = (
    "rise",
    "drop",
    "slide-left",
    "slide-right",
    "scale-in",
    "scale-out",
    "blur-in",
    "blur-out",
    "wipe-up",
    "wipe-down",
    "wipe-left",
    "wipe-right",
    "curtain-x",
    "unfold-y",
    "orbit-in",
    "spiral-in",
    "zoom-through",
    "flip-y",
    "flip-x",
    "skew-in",
    "drift-in",
    "tumble-in",
    "elastic-pop",
    "gravity-drop",
    "counter-slide",
    "settle",
    "typewriter",
    "reveal-none",
)

#: Easing curves available to the renderer (named, so the TS side can own
#: bezier definitions and stay in sync).
EASINGS: tuple[str, ...] = (
    "linear",
    "easeInQuad",
    "easeOutQuad",
    "easeInOutQuad",
    "easeOutCubic",
    "easeInOutCubic",
    "easeOutQuart",
    "easeOutExpo",
    "easeInOutExpo",
    "easeOutBack",
    "easeInOutBack",
    "easeOutElastic",
    "easeOutCirc",
    "overshoot",
    "anticipate",
    "hold-snap",
)

#: Layer kinds that carry copy a viewer is meant to READ. These get a
#: legibility floor; everything else is decoration and may be as faint as the
#: scheme likes.
TEXT_LAYER_KINDS: tuple[str, ...] = (
    "kicker",
    "headline",
    "body",
    "chip",
    "number",
    "quote",
)

#: Scenes shorter than this get their copy scheduled to land early. A 2.7 s
#: title card whose kicker does not finish entering until 55% of the way
#: through spends most of its life blank — visible in the render as a frame
#: with decor, a caption and no words. Long scenes have room to stage an
#: entrance; short ones do not.
SHORT_SCENE_SECONDS: float = 6.0

#: Latest fraction of a short scene by which every text layer must be in.
SHORT_SCENE_TEXT_DEADLINE: float = 0.55

#: Rest-state opacity floor for text layers. Rendering a full video showed
#: what a lower value costs: `body` at 0.55 composited #9FB8BC over #09191B
#: into a mid-grey — visible in the frame as "washed out", flagged by the
#: visual audit as a contrast failure. Transparency is a decor tool; using it
#: on the words is a legibility bug. Entrance ramps still start at 0 and
#: arrive here.
TEXT_OPACITY_FLOOR: float = 0.85

LAYER_KINDS: tuple[str, ...] = (
    "kicker",
    "headline",
    "body",
    "rule",
    "accent-bar",
    "orb",
    "orb-ring",
    "grid",
    "panel",
    "chip",
    "number",
    "quote-mark",
    "glyph",
    "scrim",
    "progress",
)

#: Camera paths: name -> the keyframe shape the compiler will fill in.
CAMERA_PATHS: tuple[str, ...] = (
    "static",
    "slow-push",
    "slow-pull",
    "drift-right",
    "drift-left",
    "tilt-up",
    "tilt-down",
    "breath",
    "snap-push",
    "orbit-ish",
    "settle-focus",
    "handheld",
)

SIGNATURE_MOMENTS: tuple[str, ...] = (
    "luma-sweep",
    "seam-split",
    "shadow-cascade",
    "counter-fade",
    "focus-rack",
    "accent-flare",
    "type-shuffle",
    "parallax-burst",
    "ribbon-draw",
    "settle-bounce",
    "chromatic-punch",
    "bloom-through",
    "none",
)

TEXTURE_TREATMENTS: tuple[str, ...] = (
    "none",
    "grain-soft",
    "grain-heavy",
    "vignette",
    "scanline",
    "halation",
    "noise-blend",
)

#: Typographic treatments — separate axis from the scheme. A
#: ``stacked-editorial`` scene set in ``outline-overlay`` reads completely
#: differently from the same scheme in ``boxed-lower-third``.
TYPE_TREATMENTS: tuple[str, ...] = (
    "plain",
    "big-number",
    "kicker-cap",
    "outline-overlay",
    "boxed-lower-third",
    "split-justify",
    "vertical-rail",
    "oversized-crop",
    "quote-plate",
    "list-index",
)

#: How a scene classifies its content, choosing which schemes are legal.
_CONTENT_CLASSES: dict[str, tuple[str, ...]] = {
    "hero": ("monolith", "orbital-hero", "kinetic-split", "type-storm", "ribbon-weave",
             "curtain-call", "focus-pull"),
    "statement": ("stacked-editorial", "monolith", "kinetic-split", "focus-pull",
                  "mask-parade", "curtain-call", "ribbon-weave"),
    "list": ("grid-cascade", "mask-parade", "stacked-editorial", "depth-pan",
             "type-storm", "curtain-call"),
    "data": ("data-reveal", "grid-cascade", "monolith", "depth-pan", "focus-pull"),
    "quote": ("focus-pull", "monolith", "mask-parade", "ribbon-weave", "curtain-call"),
    "transition": ("depth-pan", "curtain-call", "ribbon-weave", "focus-pull",
                   "type-storm"),
}

# ---------------------------------------------------------------------------
# Scheme programs — each returns the *structure* (layers + camera + timing),
# before seeded decoration. Structure is authored here, variety comes from
# the pools; that split is what keeps output coherent AND non-repeating.
# ---------------------------------------------------------------------------


def _layer(kind: str, **kw: Any) -> dict[str, Any]:
    base = {
        "id": f"{kind}-{kw.pop('slot', 0)}",
        "kind": kind,
        "depth": 0.5,
        "enterAt": 0.0,
        "enterDur": 0.3,
        "exitAt": None,
        "exitDur": 0.25,
        "verb": "rise",
        "ease": "easeOutCubic",
        "distance": 48.0,
        "stagger": 0.0,
        "opacity": 1.0,
        "blend": "normal",
        "typeTreatment": "plain",
        "holdCenter": False,
        "rotate": 0.0,
        "scale": 1.0,
    }
    base.update(kw)
    return base


def _scheme_stacked_editorial(rng: random.Random, cls: str) -> dict[str, Any]:
    """Editorial stack: kicker, rule, headline, body — staggered, aligned."""
    align = rng.choice(("left", "left", "center"))
    flip = align == "left" and rng.random() < 0.5
    xdir = 1 if flip else -1
    return {
        "layers": [
            _layer("rule", slot=0, depth=0.25, enterAt=0.04, enterDur=0.22,
                   verb="wipe-left" if not flip else "wipe-right",
                   ease="easeOutExpo", distance=0.0),
            _layer("kicker", slot=0, depth=0.55, enterAt=0.10, enterDur=0.28,
                   verb="rise", ease="easeOutQuart", distance=26.0,
                   typeTreatment="kicker-cap"),
            _layer("headline", slot=0, depth=0.8, enterAt=0.16, enterDur=0.42,
                   verb="rise" if align == "center" else "slide-left",
                   ease="easeOutExpo", distance=64.0, scale=1.0),
            _layer("body", slot=0, depth=0.65, enterAt=0.30, enterDur=0.36,
                   verb="rise", ease="easeOutCubic", distance=40.0, opacity=0.94),
            _layer("accent-bar", slot=0, depth=0.35, enterAt=0.22, enterDur=0.30,
                   verb="scale-in", ease="easeOutBack", distance=xdir * 12.0),
        ],
        "camera": rng.choice(("slow-push", "breath", "drift-right", "drift-left")),
        "align": align,
        "texture": rng.choice(("grain-soft", "vignette", "none")),
    }


def _scheme_monolith(rng: random.Random, cls: str) -> dict[str, Any]:
    """One enormous type block; the scene is the letterform."""
    tilt = rng.choice((-3.5, -2.0, 2.0, 3.5))
    return {
        "layers": [
            _layer("orb", slot=0, depth=0.15, enterAt=0.0, enterDur=0.5,
                   verb="scale-out", ease="easeOutExpo", opacity=0.28,
                   blend="screen"),
            _layer("number" if cls == "data" else "headline", slot=0, depth=0.75,
                   enterAt=0.20, enterDur=0.64, verb="zoom-through",
                   ease="easeOutExpo", distance=0.0, scale=1.06, rotate=tilt,
                   typeTreatment="oversized-crop"),
            _layer("rule", slot=1, depth=0.4, enterAt=0.46, enterDur=0.26,
                   verb="wipe-left", ease="easeOutQuart", distance=0.0),
            _layer("body", slot=0, depth=0.6, enterAt=0.56, enterDur=0.30,
                   verb="blur-in", ease="easeOutCubic", distance=0.0,
                   opacity=0.9),
        ],
        "camera": rng.choice(("slow-push", "settle-focus", "snap-push")),
        "align": rng.choice(("center", "center", "left")),
        "texture": rng.choice(("grain-heavy", "halation", "vignette")),
    }


def _scheme_kinetic_split(rng: random.Random, cls: str) -> dict[str, Any]:
    """A seam opens; halves counter-slide; content lands in the gap."""
    horizontal = rng.random() < 0.65
    return {
        "layers": [
            _layer("panel", slot=0, depth=0.2, enterAt=0.0, enterDur=1.0,
                   verb="curtain-x" if horizontal else "unfold-y",
                   ease="easeInOutExpo", distance=0.0, opacity=0.5),
            _layer("headline", slot=0, depth=0.8, enterAt=0.26, enterDur=0.44,
                   verb="counter-slide", ease="easeOutExpo", distance=88.0),
            _layer("accent-bar", slot=0, depth=0.5, enterAt=0.34, enterDur=0.24,
                   verb="scale-in" if horizontal else "scale-out",
                   ease="easeOutBack", distance=0.0),
            _layer("body", slot=0, depth=0.62, enterAt=0.46, enterDur=0.34,
                   verb="drop", ease="easeOutCubic", distance=34.0, opacity=0.93),
            _layer("kicker", slot=0, depth=0.45, enterAt=0.58, enterDur=0.24,
                   verb="drift-in", ease="easeOutQuad", distance=18.0,
                   typeTreatment="kicker-cap"),
        ],
        "camera": rng.choice(("snap-push", "breath", "static", "drift-right")),
        "align": rng.choice(("left", "center")),
        "texture": rng.choice(("scanline", "grain-soft", "none")),
    }


def _scheme_depth_pan(rng: random.Random, cls: str) -> dict[str, Any]:
    """Parallax: layers sit at different depths and pan at different rates."""
    dirn = rng.choice((-1, 1))
    return {
        "layers": [
            _layer("grid", slot=0, depth=0.1, enterAt=0.0, enterDur=0.4,
                   verb="slide-right" if dirn > 0 else "slide-left",
                   ease="linear", distance=0.0, opacity=0.16),
            _layer("orb", slot=1, depth=0.2, enterAt=0.05, enterDur=0.6,
                   verb="drift-in", ease="easeOutQuad", opacity=0.3,
                   blend="screen"),
            _layer("headline", slot=0, depth=0.85, enterAt=0.18, enterDur=0.46,
                   verb="slide-right" if dirn > 0 else "slide-left",
                   ease="easeOutExpo", distance=110.0),
            _layer("body", slot=0, depth=0.6, enterAt=0.36, enterDur=0.36,
                   verb="slide-right" if dirn > 0 else "slide-left",
                   ease="easeOutCubic", distance=-46.0, opacity=0.92),
            _layer("rule", slot=0, depth=0.45, enterAt=0.28, enterDur=0.28,
                   verb="wipe-left", ease="easeOutQuart", distance=0.0),
        ],
        "camera": rng.choice(("drift-right", "drift-left", "orbit-ish", "handheld")),
        "align": rng.choice(("left", "left", "right")),
        "texture": rng.choice(("grain-soft", "vignette", "noise-blend")),
    }


def _scheme_grid_cascade(rng: random.Random, cls: str) -> dict[str, Any]:
    """Items materialise as a cascade over an implied grid."""
    columns = rng.choice((2, 3, 3, 4))
    return {
        "layers": [
            _layer("grid", slot=0, depth=0.12, enterAt=0.0, enterDur=0.36,
                   verb="blur-in", ease="easeOutQuad", opacity=0.14),
            _layer("headline", slot=0, depth=0.8, enterAt=0.08, enterDur=0.34,
                   verb="rise", ease="easeOutExpo", distance=52.0),
            _layer("chip", slot=0, depth=0.6, enterAt=0.30, enterDur=0.26,
                   verb="elastic-pop", ease="easeOutBack", stagger=0.085,
                   distance=0.0, blend="normal"),
            _layer("body", slot=0, depth=0.55, enterAt=0.62, enterDur=0.30,
                   verb="rise", ease="easeOutCubic", distance=30.0, opacity=0.9,
                   typeTreatment="list-index"),
            _layer("accent-bar", slot=0, depth=0.4, enterAt=0.20, enterDur=0.22,
                   verb="wipe-up", ease="easeOutQuart", distance=0.0),
        ],
        "camera": rng.choice(("static", "slow-push", "tilt-down", "breath")),
        "align": rng.choice(("left", "center")),
        "columns": columns,
        "texture": rng.choice(("none", "grain-soft", "vignette")),
    }


def _scheme_mask_parade(rng: random.Random, cls: str) -> dict[str, Any]:
    """Masked bands sweep in sequence, each revealing a line."""
    vertical = rng.random() < 0.7
    return {
        "layers": [
            _layer("scrim", slot=0, depth=0.18, enterAt=0.0, enterDur=0.4,
                   verb="blur-in", ease="easeOutQuad", opacity=0.4),
            _layer("kicker", slot=0, depth=0.5, enterAt=0.06, enterDur=0.22,
                   verb="wipe-up" if vertical else "wipe-left",
                   ease="easeOutExpo", distance=0.0, typeTreatment="kicker-cap"),
            _layer("headline", slot=0, depth=0.8, enterAt=0.16, enterDur=0.34,
                   verb="wipe-up" if vertical else "wipe-left",
                   ease="easeOutExpo", distance=0.0),
            _layer("body", slot=0, depth=0.62, enterAt=0.34, enterDur=0.30,
                   verb="wipe-up" if vertical else "wipe-left",
                   ease="easeOutCubic", distance=0.0, stagger=0.06, opacity=0.93),
            _layer("accent-bar", slot=0, depth=0.35, enterAt=0.52, enterDur=0.26,
                   verb="wipe-right", ease="easeOutQuart", distance=0.0),
        ],
        "camera": rng.choice(("static", "breath", "settle-focus", "slow-pull")),
        "align": rng.choice(("left", "center")),
        "texture": rng.choice(("scanline", "none", "grain-soft")),
    }


def _scheme_orbital_hero(rng: random.Random, cls: str) -> dict[str, Any]:
    """Hero type arrives on an arc; orbit rings counter-rotate behind it."""
    spin = rng.choice((-1, 1))
    return {
        "layers": [
            _layer("orb", slot=0, depth=0.12, enterAt=0.0, enterDur=0.7,
                   verb="scale-out", ease="easeOutExpo", opacity=0.32,
                   blend="screen"),
            _layer("orb-ring", slot=0, depth=0.2, enterAt=0.05, enterDur=0.8,
                   verb="spiral-in", ease="easeOutCirc", opacity=0.4,
                   rotate=spin * 18.0, blend="screen"),
            _layer("orb-ring", slot=1, depth=0.16, enterAt=0.12, enterDur=0.8,
                   verb="orbit-in", ease="easeOutCirc", opacity=0.28,
                   rotate=-spin * 26.0, blend="screen"),
            _layer("headline", slot=0, depth=0.85, enterAt=0.22, enterDur=0.52,
                   verb="orbit-in", ease="easeOutExpo", distance=96.0,
                   typeTreatment=rng.choice(("plain", "outline-overlay", "boxed-lower-third"))),
            _layer("body", slot=0, depth=0.6, enterAt=0.50, enterDur=0.34,
                   verb="blur-in", ease="easeOutCubic", distance=0.0, opacity=0.9),
            _layer("rule", slot=0, depth=0.35, enterAt=0.42, enterDur=0.26,
                   verb="wipe-left", ease="easeOutQuart", distance=0.0),
        ],
        "camera": rng.choice(("orbit-ish", "slow-push", "breath", "handheld")),
        "align": rng.choice(("center", "center", "left")),
        "texture": rng.choice(("halation", "vignette", "bloom-through")),
    }


def _scheme_focus_pull(rng: random.Random, cls: str) -> dict[str, Any]:
    """Everything starts soft; focus racks onto the one line that matters."""
    return {
        "layers": [
            _layer("orb", slot=0, depth=0.15, enterAt=0.0, enterDur=0.6,
                   verb="blur-out", ease="easeOutQuad", opacity=0.34,
                   blend="screen"),
            _layer("body", slot=0, depth=0.5, enterAt=0.04, enterDur=0.3,
                   verb="blur-in", ease="easeOutQuad", distance=0.0, opacity=0.55),
            _layer("kicker", slot=0, depth=0.6, enterAt=0.10, enterDur=0.24,
                   verb="blur-in", ease="easeOutCubic", distance=0.0,
                   typeTreatment="kicker-cap"),
            _layer("headline", slot=0, depth=0.9, enterAt=0.26, enterDur=0.40,
                   verb="blur-in", ease="easeInOutExpo", distance=0.0, scale=0.985),
            _layer("accent-bar", slot=0, depth=0.4, enterAt=0.48, enterDur=0.24,
                   verb="scale-in", ease="easeOutExpo", distance=0.0),
        ],
        "camera": rng.choice(("settle-focus", "slow-push", "static", "breath")),
        "align": rng.choice(("center", "left")),
        "texture": rng.choice(("halation", "grain-soft", "none")),
    }


def _scheme_ribbon_weave(rng: random.Random, cls: str) -> dict[str, Any]:
    """Diagonal ribbons draw across the frame; type rides them."""
    segs = rng.choice((2, 3, 4))
    return {
        "layers": [
            _layer("glyph", slot=0, depth=0.16, enterAt=0.0, enterDur=0.5,
                   verb="wipe-right", ease="easeOutQuart", opacity=0.35,
                   rotate=-14.0, blend="screen"),
            _layer("glyph", slot=1, depth=0.22, enterAt=0.10, enterDur=0.5,
                   verb="wipe-left", ease="easeOutQuart", opacity=0.28,
                   rotate=11.0, blend="screen"),
            _layer("headline", slot=0, depth=0.82, enterAt=0.20, enterDur=0.44,
                   verb="counter-slide", ease="easeOutExpo", distance=72.0),
            _layer("body", slot=0, depth=0.6, enterAt=0.44, enterDur=0.32,
                   verb="rise", ease="easeOutCubic", distance=36.0, opacity=0.92),
            _layer("accent-bar", slot=0, depth=0.45, enterAt=0.34, enterDur=0.28,
                   verb="wipe-right", ease="easeOutExpo", distance=0.0,
                   rotate=-14.0),
        ],
        "camera": rng.choice(("drift-left", "drift-right", "handheld", "slow-push")),
        "align": rng.choice(("left", "left", "center")),
        "ribbonSegments": segs,
        "texture": rng.choice(("grain-soft", "halation", "vignette")),
    }


def _scheme_data_reveal(rng: random.Random, cls: str) -> dict[str, Any]:
    """A number lands, then its meaning assembles around it."""
    return {
        "layers": [
            _layer("grid", slot=0, depth=0.12, enterAt=0.0, enterDur=0.4,
                   verb="blur-in", ease="easeOutQuad", opacity=0.15),
            _layer("number", slot=0, depth=0.85, enterAt=0.14, enterDur=0.40,
                   verb="zoom-through", ease="easeOutExpo", scale=1.04,
                   typeTreatment="big-number"),
            _layer("progress", slot=0, depth=0.5, enterAt=0.40, enterDur=0.42,
                   verb="wipe-left", ease="easeOutExpo", distance=0.0),
            _layer("body", slot=0, depth=0.6, enterAt=0.46, enterDur=0.32,
                   verb="rise", ease="easeOutCubic", distance=34.0, opacity=0.92),
            _layer("accent-bar", slot=0, depth=0.4, enterAt=0.32, enterDur=0.24,
                   verb="scale-in", ease="easeOutBack", distance=0.0),
        ],
        "camera": rng.choice(("static", "snap-push", "settle-focus", "breath")),
        "align": rng.choice(("center", "left")),
        "texture": rng.choice(("grain-soft", "scanline", "none")),
    }


def _scheme_curtain_call(rng: random.Random, cls: str) -> dict[str, Any]:
    """A curtain parts; the scene is revealed rather than assembled."""
    vertical = rng.random() < 0.5
    return {
        "layers": [
            _layer("panel", slot=0, depth=0.22, enterAt=0.0, enterDur=1.0,
                   verb="unfold-y" if vertical else "curtain-x",
                   ease="easeInOutCubic", opacity=0.55),
            _layer("headline", slot=0, depth=0.82, enterAt=0.30, enterDur=0.40,
                   verb="scale-in", ease="easeOutExpo", distance=0.0, scale=0.94),
            _layer("kicker", slot=0, depth=0.55, enterAt=0.22, enterDur=0.26,
                   verb="rise", ease="easeOutQuart", distance=22.0,
                   typeTreatment="kicker-cap"),
            _layer("body", slot=0, depth=0.6, enterAt=0.48, enterDur=0.32,
                   verb="rise", ease="easeOutCubic", distance=32.0, opacity=0.92),
            _layer("rule", slot=0, depth=0.4, enterAt=0.60, enterDur=0.24,
                   verb="wipe-right", ease="easeOutExpo", distance=0.0),
        ],
        "camera": rng.choice(("slow-pull", "breath", "static", "tilt-up")),
        "align": rng.choice(("center", "left")),
        "texture": rng.choice(("vignette", "halation", "grain-heavy")),
    }


def _scheme_type_storm(rng: random.Random, cls: str) -> dict[str, Any]:
    """Many small type elements cascade in, then settle into one hero line."""
    return {
        "layers": [
            _layer("glyph", slot=0, depth=0.2, enterAt=0.02, enterDur=0.4,
                   verb="tumble-in", ease="easeOutQuad", stagger=0.055,
                   opacity=0.34, blend="screen"),
            _layer("chip", slot=0, depth=0.28, enterAt=0.10, enterDur=0.36,
                   verb="gravity-drop", ease="easeOutBack", stagger=0.06,
                   opacity=0.5),
            _layer("headline", slot=0, depth=0.85, enterAt=0.34, enterDur=0.42,
                   verb="settle", ease="easeOutElastic", distance=0.0,
                   scale=1.02),
            _layer("accent-bar", slot=0, depth=0.45, enterAt=0.52, enterDur=0.24,
                   verb="wipe-left", ease="easeOutExpo", distance=0.0),
            _layer("body", slot=0, depth=0.6, enterAt=0.58, enterDur=0.30,
                   verb="rise", ease="easeOutCubic", distance=28.0, opacity=0.9),
        ],
        "camera": rng.choice(("handheld", "snap-push", "breath", "tilt-down")),
        "align": rng.choice(("center", "left")),
        "texture": rng.choice(("noise-blend", "grain-heavy", "scanline")),
    }


_SCHEME_PROGRAMS = {
    "stacked-editorial": _scheme_stacked_editorial,
    "monolith": _scheme_monolith,
    "kinetic-split": _scheme_kinetic_split,
    "depth-pan": _scheme_depth_pan,
    "grid-cascade": _scheme_grid_cascade,
    "mask-parade": _scheme_mask_parade,
    "orbital-hero": _scheme_orbital_hero,
    "focus-pull": _scheme_focus_pull,
    "ribbon-weave": _scheme_ribbon_weave,
    "data-reveal": _scheme_data_reveal,
    "curtain-call": _scheme_curtain_call,
    "type-storm": _scheme_type_storm,
}


# ---------------------------------------------------------------------------
# Camera path compilation
# ---------------------------------------------------------------------------

def _camera_keys(path: str, rng: random.Random, drift: float) -> list[dict[str, float]]:
    """Compile a camera path name into concrete keyframes.

    Positions are fractions of scene duration; ``zoom`` is a multiplier,
    ``x``/``y`` are fractions of frame size, ``rotate`` is degrees. The
    renderer interpolates between keys, so a path with two keys is a
    straight move and five keys is a curve.
    """
    d = max(0.0, min(1.0, drift))           # brand camera_drift 0..1
    j = lambda a: round(a * (0.85 + 0.3 * rng.random()), 4)  # noqa: E731
    if path == "static":
        return [{"at": 0.0, "zoom": 1.0, "x": 0, "y": 0, "rotate": 0}]
    if path == "slow-push":
        return [{"at": 0.0, "zoom": 1.0, "x": 0, "y": 0, "rotate": 0},
                {"at": 1.0, "zoom": 1.0 + j(0.06 * (0.5 + d)), "x": 0, "y": j(-0.01), "rotate": 0}]
    if path == "slow-pull":
        return [{"at": 0.0, "zoom": 1.0 + j(0.06 * (0.5 + d)), "x": 0, "y": 0, "rotate": 0},
                {"at": 1.0, "zoom": 1.0, "x": 0, "y": 0, "rotate": 0}]
    if path == "drift-right":
        return [{"at": 0.0, "zoom": 1.03, "x": j(-0.02 * (0.5 + d)), "y": 0, "rotate": 0},
                {"at": 1.0, "zoom": 1.03, "x": j(0.02 * (0.5 + d)), "y": j(0.008), "rotate": 0}]
    if path == "drift-left":
        return [{"at": 0.0, "zoom": 1.03, "x": j(0.02 * (0.5 + d)), "y": 0, "rotate": 0},
                {"at": 1.0, "zoom": 1.03, "x": j(-0.02 * (0.5 + d)), "y": j(0.008), "rotate": 0}]
    if path == "tilt-up":
        return [{"at": 0.0, "zoom": 1.04, "x": 0, "y": j(0.02 * (0.5 + d)), "rotate": 0},
                {"at": 1.0, "zoom": 1.02, "x": 0, "y": j(-0.015 * (0.5 + d)), "rotate": 0}]
    if path == "tilt-down":
        return [{"at": 0.0, "zoom": 1.04, "x": 0, "y": j(-0.02 * (0.5 + d)), "rotate": 0},
                {"at": 1.0, "zoom": 1.02, "x": 0, "y": j(0.015 * (0.5 + d)), "rotate": 0}]
    if path == "breath":
        return [{"at": 0.0, "zoom": 1.0, "x": 0, "y": 0, "rotate": 0},
                {"at": 0.5, "zoom": 1.0 + j(0.025 * (0.4 + d)), "x": j(0.004), "y": j(-0.004), "rotate": 0},
                {"at": 1.0, "zoom": 1.0, "x": 0, "y": 0, "rotate": 0}]
    if path == "snap-push":
        return [{"at": 0.0, "zoom": 1.0, "x": 0, "y": 0, "rotate": 0},
                {"at": 0.18, "zoom": 1.0 + j(0.075 * (0.5 + d)), "x": 0, "y": 0, "rotate": 0},
                {"at": 1.0, "zoom": 1.0 + j(0.03 * (0.5 + d)), "x": j(-0.006), "y": 0, "rotate": 0}]
    if path == "orbit-ish":
        r = j(0.022 * (0.4 + d))
        return [{"at": 0.0, "zoom": 1.03, "x": r, "y": 0, "rotate": j(-0.6)},
                {"at": 0.33, "zoom": 1.035, "x": 0, "y": r, "rotate": 0},
                {"at": 0.66, "zoom": 1.03, "x": -r, "y": 0, "rotate": j(0.6)},
                {"at": 1.0, "zoom": 1.03, "x": 0, "y": -r, "rotate": 0}]
    if path == "settle-focus":
        return [{"at": 0.0, "zoom": 1.0 + j(0.09 * (0.5 + d)), "x": j(0.012), "y": j(-0.01), "rotate": 0},
                {"at": 0.55, "zoom": 1.025, "x": j(0.003), "y": 0, "rotate": 0},
                {"at": 1.0, "zoom": 1.0, "x": 0, "y": 0, "rotate": 0}]
    if path == "handheld":
        keys = [{"at": 0.0, "zoom": 1.03, "x": 0, "y": 0, "rotate": 0}]
        amps = [0.006 * (0.4 + d), 0.004 * (0.4 + d)]
        for i, at in enumerate((0.22, 0.46, 0.70, 1.0), start=1):
            keys.append({
                "at": at,
                "zoom": round(1.03 + j(0.012), 4),
                "x": round(rng.uniform(-amps[0], amps[0]), 4),
                "y": round(rng.uniform(-amps[1], amps[1]), 4),
                "rotate": round(rng.uniform(-0.35, 0.35), 4),
            })
        return keys
    return [{"at": 0.0, "zoom": 1.0, "x": 0, "y": 0, "rotate": 0}]


# ---------------------------------------------------------------------------
# Content classification + palette
# ---------------------------------------------------------------------------

def classify_content(section: dict[str, Any]) -> str:
    """Choose which schemes are legal for this section's content."""
    text = " ".join(
        str(section.get(k) or "") for k in ("heading", "text", "kicker", "body")
    )
    low = text.lower()
    explicit = str(section.get("scene_class") or section.get("content_class") or "").lower()
    if explicit in _CONTENT_CLASSES:
        return explicit
    if section.get("stat") or section.get("number"):
        return "data"
    # A leading number/datum ("47% of teams…", "$1.2M raised") reads as data.
    import re

    if re.match(r"^\s*[$€£]?\s*\d", text):
        return "data"
    if any(t in low for t in (
        "step 1", "step 2", "first,", "second,", "finally,", "three things",
        "here are", "the list", "checklist",
    )):
        return "list"
    if text.count("\n") >= 2 or len(re.findall(r"(?:^|\n)\s*[-•*]\s", text)) >= 2:
        return "list"
    if text.strip().startswith(("\u201c", '"')) or section.get("quote"):
        return "quote"
    if section.get("hero") or section.get("kind") == "hero":
        return "hero"
    return "statement"


def palette_for_spec(
    brand: Any, section_palette: Optional[dict[str, Any]], index: int
) -> dict[str, str]:
    """Resolve the palette block the renderer paints with.

    Prefers the per-section palette (so the video's visual world evolves)
    and falls back to the channel brand (so an unbranded run still looks
    intentional rather than default-grey).
    """
    sp = section_palette or {}
    accent = str(sp.get("accent") or (brand.accent if brand else "#4E9AF1"))
    text = str(sp.get("text") or (brand.fg if brand else "#F7F9FF"))
    bg_base = str(sp.get("bgBase") or (brand.bg if brand else "#0B0E1A"))
    muted = str(sp.get("muted") or _mix_hex(text, bg_base, 0.42))

    # Legibility is not a design opinion. Body copy and kickers are the
    # narration made visible; if they fall below WCAG AA on the section's own
    # background they get lifted until they pass. The visual audit measures
    # the rendered result, so this is the pre-emptive half of that check.
    #
    # Checked at TEXT_OPACITY_FLOOR rather than at 1.0, because 1.0 is not
    # what gets drawn: the layer is composited with the background, and a
    # swatch that passes on paper came out as mid-grey on screen at the
    # opacities the schemes used to author.
    text = ensure_contrast_at_alpha(text, bg_base, 1.0, 4.5)
    muted = ensure_contrast_at_alpha(muted, bg_base, TEXT_OPACITY_FLOOR, 4.5)

    return {
        "bgBase": bg_base,
        "bgAlt": str(sp.get("bgAlt") or _shift_hex(bg_base, 0.10)),
        "glowA": str(sp.get("glowA") or accent),
        "glowB": str(sp.get("glowB") or (brand.primary if brand else "#1B2A4A")),
        "accent": accent,
        "text": text,
        "muted": muted,
        "glass": str(sp.get("glass") or "rgba(12, 16, 32, 0.62)"),
        "glassEdge": str(sp.get("glassEdge") or _rgba_from_hex(accent, 0.30)),
    }


def _rel_luma(hex_color: str) -> float:
    """WCAG relative luminance."""
    h = (hex_color or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return 0.0
    try:
        chan = [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    except ValueError:
        return 0.0

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in chan)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    la, lb = _rel_luma(a), _rel_luma(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def ensure_contrast(color: str, against: str, minimum: float = 4.5) -> str:
    """Lighten ``color`` until it clears ``minimum`` contrast against ``against``.

    The palettes the director generates are tuned for *glow* legibility, not
    for text. Rendering the first full video showed the cost: body copy came
    out at 0.15 p05-p95 luma separation and the visual audit flagged it —
    unreadable narration is a defect no amount of choreography excuses.
    This walks the colour toward white until it passes, so a compiled scene
    is always readable on the palette it was given.
    """
    if contrast_ratio(color, against) >= minimum:
        return color
    h = (color or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return "#F7F9FF"
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "#F7F9FF"
    for step in range(1, 21):
        t = step / 20.0
        nr = int(round(r + (255 - r) * t))
        ng = int(round(g + (255 - g) * t))
        nb = int(round(b + (255 - b) * t))
        candidate = f"#{nr:02X}{ng:02X}{nb:02X}"
        if contrast_ratio(candidate, against) >= minimum:
            return candidate
    return "#F7F9FF"


def composite_over(color: str, bg: str, alpha: float) -> str:
    """What ``color`` at ``alpha`` actually looks like painted on ``bg``.

    A palette can pass WCAG at full opacity and still be unreadable, because
    the layer is drawn at 0.85 — the browser composites it with the
    background and the result is a different colour. Measured on a real
    render: ``#9FB8BC`` on ``#09191B`` is 9.5:1, but at the 0.55 the compiler
    used to emit it composited to a mid-grey that the visual audit flagged.
    Contrast has to be checked on the composite, not the swatch.
    """
    a = max(0.0, min(1.0, float(alpha)))

    def _rgb(h: str) -> tuple[int, int, int]:
        h = (h or "").lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            return (0, 0, 0)
        try:
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
        except ValueError:
            return (0, 0, 0)

    fr, fg, fb = _rgb(color)
    br, bg_, bb = _rgb(bg)
    return "#{:02X}{:02X}{:02X}".format(
        int(round(fr * a + br * (1 - a))),
        int(round(fg * a + bg_ * (1 - a))),
        int(round(fb * a + bb * (1 - a))),
    )


def ensure_contrast_at_alpha(
    color: str, against: str, alpha: float, minimum: float = 4.5
) -> str:
    """Lighten ``color`` until it clears ``minimum`` **as composited** at ``alpha``.

    The stronger form of :func:`ensure_contrast`: it accounts for the opacity
    the renderer will actually paint with, so a palette cannot pass the check
    and still come out washed out on screen.
    """
    if contrast_ratio(composite_over(color, against, alpha), against) >= minimum:
        return color
    h = (color or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return "#F7F9FF"
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "#F7F9FF"
    for step in range(1, 41):
        t = step / 40.0
        candidate = "#{:02X}{:02X}{:02X}".format(
            int(round(r + (255 - r) * t)),
            int(round(g + (255 - g) * t)),
            int(round(b + (255 - b) * t)),
        )
        if contrast_ratio(composite_over(candidate, against, alpha), against) >= minimum:
            return candidate
    return "#F7F9FF"


def _shift_hex(hex_color: str, amount: float) -> str:
    h = (hex_color or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return "#151B3B"
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "#151B3B"
    f = 1 + amount if amount < 0 else 1
    if amount >= 0:  # lighten
        r, g, b = (int(round(c + (255 - c) * amount)) for c in (r, g, b))
    else:
        r, g, b = (int(round(c * f)) for c in (r, g, b))
    return f"#{max(0, min(255, r)):02X}{max(0, min(255, g)):02X}{max(0, min(255, b)):02X}"


def _mix_hex(a: str, b: str, t: float) -> str:
    def parse(x: str) -> tuple[int, int, int]:
        x = (x or "").lstrip("#")
        if len(x) == 3:
            x = "".join(c * 2 for c in x)
        if len(x) != 6:
            return (127, 127, 127)
        try:
            return tuple(int(x[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
        except ValueError:
            return (127, 127, 127)

    ar, ag, ab = parse(a)
    br, bg, bb = parse(b)
    return "#{:02X}{:02X}{:02X}".format(
        int(round(ar + (br - ar) * t)),
        int(round(ag + (bg - ag) * t)),
        int(round(ab + (bb - ab) * t)),
    )


def _rgba_from_hex(hex_color: str, alpha: float) -> str:
    h = (hex_color or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return "rgba(78, 154, 241, 0.30)"
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "rgba(78, 154, 241, 0.30)"
    return f"rgba({r}, {g}, {b}, {alpha:.2f})"


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------

def _seed_for(project_id: str, channel_id: str, index: int, salt: str = "") -> int:
    """Stable per-scene seed: retries identical, videos different."""
    material = f"{channel_id}:{project_id}:{index}:{salt}"
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


# ---------------------------------------------------------------------------
# The compiler
# ---------------------------------------------------------------------------

def compile_scene(
    section: dict[str, Any],
    brand: Any,
    project_id: str,
    *,
    index: int = 0,
    duration: float = 8.0,
    section_palette: Optional[dict[str, Any]] = None,
    scheme: Optional[str] = None,
    continuity: Optional[dict[str, Any]] = None,
    mood: Optional[str] = None,
) -> dict[str, Any]:
    """Compile one section into a bespoke, renderable choreography spec.

    Deterministic for a given ``(channel, project_id, index)`` triple, so a
    Kaggle retry reproduces the frame-identical scene, while a different
    project gets a different program.

    ``continuity`` carries the previous scene's closing state so this scene
    can open where the last one ended (the Fable shared-frame rule).
    """
    rng = random.Random(_seed_for(project_id, getattr(brand, "channel_id", "default"),
                                  index, mood or ""))
    cls = classify_content(section)

    allowed = _CONTENT_CLASSES.get(cls, _CONTENT_CLASSES["statement"])
    chosen_scheme = scheme if scheme in SCHEMES else rng.choice(allowed)
    program = _SCHEME_PROGRAMS[chosen_scheme](rng, cls)

    # --- seeded decoration over the scheme's structure -------------------
    layers: list[dict[str, Any]] = []
    for i, layer in enumerate(program["layers"]):
        lyr = dict(layer)
        lyr["id"] = f"{lyr['kind']}-l{i}"
        # Motion verb: keep the scheme's authored choice most of the time,
        # reseed occasionally so two runs of the same scheme differ.
        if rng.random() < 0.32 and lyr["kind"] not in ("panel", "scrim", "grid"):
            lyr["verb"] = rng.choice(MOTION_VERBS)
        if rng.random() < 0.45:
            lyr["ease"] = rng.choice(EASINGS)
        # Windows stay fractional; jitter keeps the stagger from being
        # identical across scenes without breaking pacing.
        lyr["enterAt"] = round(min(0.80, max(0.0, lyr["enterAt"] + rng.uniform(-0.03, 0.03))), 4)
        lyr["enterDur"] = round(min(0.85, max(0.10, lyr["enterDur"] * rng.uniform(0.85, 1.20))), 4)
        lyr["depth"] = round(min(1.0, max(0.0, lyr["depth"] + rng.uniform(-0.06, 0.06))), 4)
        lyr["distance"] = round(lyr["distance"] * rng.uniform(0.8, 1.25), 2)
        if lyr["kind"] == "body" and rng.random() < 0.5:
            lyr["opacity"] = round(rng.uniform(0.86, 0.97), 3)
        # Legibility floor. A scheme may author a faint body or headline;
        # composited over a dark palette that is unreadable, so the floor
        # wins. Decoration keeps whatever it was given.
        if lyr["kind"] in TEXT_LAYER_KINDS and lyr.get("opacity", 1.0) < TEXT_OPACITY_FLOOR:
            lyr["opacity"] = TEXT_OPACITY_FLOOR
        layers.append(lyr)

    # --- content coverage contract ---------------------------------------
    # A scheme decides STRUCTURE, but it must never silently drop authored
    # copy. (Caught by rendering: `ribbon-weave` has no kicker layer, so the
    # section's kicker vanished from the frame.) Missing content layers are
    # injected with sensible choreography rather than lost.
    layers = _ensure_content_coverage(layers, section, cls)

    # --- short-scene pacing ----------------------------------------------
    # Decor may take as long as it likes (a panel that curtains across the
    # whole scene is a look). Copy may not: on a short scene, anything that
    # has not arrived by the deadline is compressed to fit, preserving the
    # scheme's ORDER while removing the dead air.
    if float(duration) < SHORT_SCENE_SECONDS:
        late = [lyr for lyr in layers
                if lyr["kind"] in TEXT_LAYER_KINDS
                and lyr["enterAt"] + lyr["enterDur"] > SHORT_SCENE_TEXT_DEADLINE]
        if late:
            latest = max(lyr["enterAt"] + lyr["enterDur"] for lyr in late)
            scale = SHORT_SCENE_TEXT_DEADLINE / latest if latest > 0 else 1.0
            for lyr in late:
                lyr["enterAt"] = round(lyr["enterAt"] * scale, 4)
                lyr["enterDur"] = round(max(0.12, lyr["enterDur"] * scale), 4)

    # Most scenes get a signature moment; a few deliberately do not, so the
    # gesture does not become its own template.
    signature = rng.choice(SIGNATURE_MOMENTS) if rng.random() < 0.72 else "none"
    if signature != "none" and program.get("ribbonSegments") and signature == "ribbon-draw":
        signature = "ribbon-draw"  # coherent with the scheme
    camera_path = program.get("camera", "breath")
    drift = float(getattr(brand, "camera_drift", 0.6) or 0.0)

    treatment = rng.choice(TEXTURE_TREATMENTS)
    if brand is not None and getattr(brand, "pacing", "") == "energetic":
        # Energetic channels bias to snappier verbs and lighter texture.
        layers = [{**lyr, "ease": rng.choice(("easeOutExpo", "easeOutQuart", "easeOutCirc"))}
                  for lyr in layers]

    type_treatment = rng.choice(TYPE_TREATMENTS) if rng.random() < 0.55 else "plain"

    spec: dict[str, Any] = {
        "version": SPEC_VERSION,
        "id": f"scene-{index}-{chosen_scheme}",
        "scheme": chosen_scheme,
        "contentClass": cls,
        "durationSeconds": round(float(duration), 3),
        "align": program.get("align", "left"),
        "columns": int(program.get("columns", 3)),
        "ribbonSegments": int(program.get("ribbonSegments", 3)),
        "typeTreatment": type_treatment,
        "palette": palette_for_spec(brand, section_palette, index),
        "camera": {
            "path": camera_path,
            "keys": _camera_keys(camera_path, rng, drift),
            "depthCoupling": round(rng.uniform(0.3, 0.85), 3),
        },
        "layers": layers,
        "signature": {
            "moment": signature,
            "at": round(rng.uniform(0.30, 0.62), 3),
            "intensity": round(rng.uniform(0.35, 0.95), 3),
        },
        "texture": {"treatment": treatment, "amount": round(rng.uniform(0.15, 0.55), 3)},
        "continuity": {
            "openOn": (continuity or {}).get("closeOn") or None,
            "closeOn": {"accent": spec_accent(section_palette, brand), "align": program.get("align", "left")},
        },
        "text": {
            "kicker": str(section.get("kicker") or ""),
            "headline": str(section.get("heading") or section.get("title") or ""),
            "body": str(section.get("text") or section.get("body") or ""),
            "stat": str(section.get("stat") or section.get("number") or ""),
            "subtitle": str(section.get("subtitle") or ""),
        },
        "media": _media_block(section),
        "seed": _seed_for(project_id, getattr(brand, "channel_id", "default"), index, mood or ""),
    }
    return spec


def _media_block(section: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Optional footage under a compiled scene (the hero b-roll path).

    A compiled scene that can carry footage means the hero cuts stop being a
    repeating component too — the last place the old template vocabulary
    survived in every section of every video.
    """
    src = (section.get("backgroundVideo") or section.get("backgroundImage")
           or section.get("media_src") or "")
    if not src:
        return None
    is_video = str(src).lower().endswith((".mp4", ".mov", ".webm", ".mkv", ".avi"))
    return {
        "src": str(src),
        "kind": "video" if is_video else "image",
        "startSeconds": float(section.get("backgroundVideoStart") or 0.0),
        "sourceDuration": float(section.get("backgroundVideoDuration") or 0.0),
        "overlay": float(section.get("backgroundOverlay") or 0.5),
    }


def _ensure_content_coverage(
    layers: list[dict[str, Any]], section: dict[str, Any], cls: str
) -> list[dict[str, Any]]:
    """Guarantee every piece of authored copy has a layer that renders it.

    Schemes are structural; this is the correctness backstop. Without it a
    scheme that omits (say) a kicker silently deletes the writer's words —
    a content bug wearing a design costume. Injected layers are marked so
    QC can tell authored structure from backstop additions.
    """
    kinds = {lyr["kind"] for lyr in layers}
    out = list(layers)

    def _last_text_end() -> float:
        ends = [
            float(lyr["enterAt"]) + float(lyr["enterDur"])
            for lyr in out
            if lyr["kind"] in ("headline", "body", "number", "chip", "kicker")
        ]
        return min(0.62, max(ends) if ends else 0.2)

    kicker = str(section.get("kicker") or "")
    heading = str(section.get("heading") or section.get("title") or "")
    body = str(section.get("text") or section.get("body") or "")
    stat = str(section.get("stat") or section.get("number") or "")

    # `number` covers headline when the section is datum-shaped.
    headline_covered = "headline" in kinds or (
        "number" in kinds and bool(stat or heading)
    )
    if heading and not headline_covered:
        out.append(_layer(
            "headline", slot=9, depth=0.8,
            enterAt=round(_last_text_end() * 0.5, 3), enterDur=0.42,
            verb="rise", ease="easeOutExpo", distance=64.0,
        ))
        kinds.add("headline")

    if body and "body" not in kinds and "chip" not in kinds:
        out.append(_layer(
            "body", slot=9, depth=0.62,
            enterAt=round(_last_text_end(), 3), enterDur=0.34,
            verb="rise", ease="easeOutCubic", distance=38.0, opacity=0.93,
        ))
        kinds.add("body")

    if kicker and "kicker" not in kinds:
        out.append(_layer(
            "kicker", slot=9, depth=0.5,
            enterAt=0.08, enterDur=0.26,
            verb="rise", ease="easeOutQuart", distance=22.0,
            typeTreatment="kicker-cap",
        ))

    # Content layers render in a readable vertical order regardless of the
    # order the scheme authored them in; decorative layers keep their own
    # ordering and are positioned by the renderer.
    _order = {
        "kicker": 0, "rule": 1, "accent-bar": 2, "number": 3, "headline": 4,
        "quote-mark": 5, "chip": 6, "progress": 7, "body": 8,
    }
    out.sort(key=lambda lyr: _order.get(lyr["kind"], 99))
    return out


def spec_accent(section_palette: Optional[dict[str, Any]], brand: Any) -> str:
    if section_palette and section_palette.get("accent"):
        return str(section_palette["accent"])
    if brand is not None and getattr(brand, "accent", None):
        return str(brand.accent)
    return "#4E9AF1"


def compile_video(
    script: dict[str, Any],
    brand: Any,
    project_id: str,
    *,
    section_palettes: Optional[list[dict[str, Any]]] = None,
    durations: Optional[list[float]] = None,
    schemes: Optional[list[Optional[str]]] = None,
    force_schemes: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Compile every section into one whole-video choreography plan.

    Enforces **scheme diversity**: consecutive scenes never repeat a scheme,
    and a scheme is not reused while unused schemes remain. Diversity is
    what stops a channel's videos collapsing back into one look — the
    failure mode that template selection always produces.
    """
    sections = script.get("sections") or []
    palettes = list(section_palettes or [])
    if not palettes and brand is not None:
        # Reuse the director's palette ring so the compiled scenes paint with
        # the SAME section palettes the rest of the props use. A second
        # palette generator here would be a second source of truth.
        try:
            from lib.creative_director import CreativeDirector

            overrides = brand.director_overrides(project_id)
            director = CreativeDirector(
                seed=brand.project_seed(project_id),
                brand=overrides.get("brand"),
            )
            palettes = [
                director.palette_for_section(i, len(sections) or 1)
                for i in range(len(sections))
            ]
        except Exception:
            palettes = []

    durations = list(durations or [])
    scenes: list[dict[str, Any]] = []

    # Seeded scheme ordering: a shuffled ring guarantees every scheme is used
    # once before any repeats, and the seed varies the order per project.
    ring_rng = random.Random(_seed_for(project_id, getattr(brand, "channel_id", "default"), 0, "ring"))
    ring = list(SCHEMES)
    ring_rng.shuffle(ring)
    ring_pos = 0
    last_scheme: Optional[str] = None
    continuity: dict[str, Any] = {}

    for i, section in enumerate(sections):
        cls = classify_content(section)
        allowed = set(_CONTENT_CLASSES.get(cls, _CONTENT_CLASSES["statement"]))

        if force_schemes and i < len(force_schemes) and force_schemes[i] in SCHEMES:
            chosen = force_schemes[i]
        elif schemes and i < len(schemes) and schemes[i] in SCHEMES:
            chosen = schemes[i]  # type: ignore[assignment]
        else:
            # Advance the ring to the next scheme legal for this content class.
            chosen = None
            for _ in range(len(ring)):
                cand = ring[ring_pos % len(ring)]
                ring_pos += 1
                if cand in allowed:
                    chosen = cand
                    break
            if chosen is None:
                chosen = ring_rng.choice(sorted(allowed))
            if chosen == last_scheme and len(allowed) > 1:
                for _ in range(len(ring)):
                    cand = ring[ring_pos % len(ring)]
                    ring_pos += 1
                    if cand in allowed and cand != last_scheme:
                        chosen = cand
                        break

        dur = durations[i] if i < len(durations) else float(
            section.get("duration_s") or section.get("duration") or 8.0
        )
        pal = palettes[i] if i < len(palettes) else None
        spec = compile_scene(
            section, brand, project_id, index=i, duration=dur,
            section_palette=pal, scheme=chosen, continuity=continuity,
        )
        continuity = spec.get("continuity", {})
        last_scheme = spec["scheme"]
        scenes.append(spec)

    return {
        "version": SPEC_VERSION,
        "projectId": project_id,
        "channelId": getattr(brand, "channel_id", "default"),
        "scenes": scenes,
        "stats": {
            "scenes": len(scenes),
            "distinctSchemes": len({s["scheme"] for s in scenes}),
            "distinctTexture": len({s["texture"]["treatment"] for s in scenes}),
            "distinctCameras": len({s["camera"]["path"] for s in scenes}),
            "signatureMoments": len({s["signature"]["moment"] for s in scenes
                                     if s["signature"]["moment"] != "none"}),
        },
    }


# ---------------------------------------------------------------------------
# Lint — the contract with BespokeScene.tsx
# ---------------------------------------------------------------------------

_VALID_MOTION = set(MOTION_VERBS)
_VALID_EASE = set(EASINGS)
_VALID_KIND = set(LAYER_KINDS)
_VALID_TEXTURE = set(TEXTURE_TREATMENTS)
_VALID_CAMERA = set(CAMERA_PATHS)
_VALID_SIGNATURE = set(SIGNATURE_MOMENTS)


def lint_spec(spec: dict[str, Any]) -> list[str]:
    """Return contract violations (empty = safe to render).

    Runs before ``lint_spec`` ever reaches Remotion. A spec that fails is
    rejected and the caller falls back to the template component — exactly
    the code2motion ladder, applied to the body.
    """
    v: list[str] = []
    if not isinstance(spec, dict):
        return ["spec: not a dict"]
    if spec.get("version") != SPEC_VERSION:
        v.append(f"version: expected {SPEC_VERSION}, got {spec.get('version')!r}")

    dur = spec.get("durationSeconds")
    if not isinstance(dur, (int, float)) or dur <= 0:
        v.append(f"durationSeconds: must be > 0, got {dur!r}")

    layers = spec.get("layers")
    if not isinstance(layers, list) or not layers:
        v.append("layers: must be a non-empty list")
        layers = []

    for i, lyr in enumerate(layers):
        if not isinstance(lyr, dict):
            v.append(f"layers[{i}]: not an object")
            continue
        where = f"layers[{i}]({lyr.get('kind')})"
        if lyr.get("kind") not in _VALID_KIND:
            v.append(f"{where}: unknown kind {lyr.get('kind')!r}")
        if lyr.get("verb") not in _VALID_MOTION:
            v.append(f"{where}: unknown verb {lyr.get('verb')!r}")
        if lyr.get("ease") not in _VALID_EASE:
            v.append(f"{where}: unknown ease {lyr.get('ease')!r}")
        depth = lyr.get("depth")
        if not isinstance(depth, (int, float)) or not (0.0 <= depth <= 1.0):
            v.append(f"{where}: depth must be 0..1, got {depth!r}")
        # Text legibility floor — the one style rule that is not negotiable.
        # A faint body is not "subtle", it is unreadable on a dark palette.
        op = lyr.get("opacity", 1.0)
        if lyr.get("kind") in TEXT_LAYER_KINDS:
            if not isinstance(op, (int, float)) or op < TEXT_OPACITY_FLOOR:
                v.append(
                    f"{where}: text opacity {op!r} below floor "
                    f"{TEXT_OPACITY_FLOOR} — lower the colour, not the alpha"
                )
        for key, lo, hi in (("enterAt", 0.0, 1.0), ("enterDur", 0.0, 1.0)):
            val = lyr.get(key)
            if not isinstance(val, (int, float)) or not (lo <= val <= hi):
                v.append(f"{where}: {key} must be {lo}..{hi}, got {val!r}")
        exit_at = lyr.get("exitAt")
        if exit_at is not None and (not isinstance(exit_at, (int, float)) or not (0.0 <= exit_at <= 1.0)):
            v.append(f"{where}: exitAt must be null or 0..1, got {exit_at!r}")
        if isinstance(exit_at, (int, float)) and isinstance(lyr.get("enterAt"), (int, float)):
            if exit_at <= lyr["enterAt"]:
                v.append(f"{where}: exitAt ({exit_at}) must be after enterAt ({lyr['enterAt']})")
        ids = [layer.get("id") for layer in layers if isinstance(layer, dict)]
    if len(set(ids)) != len(ids):
        v.append("layers: duplicate layer ids")

    cam = spec.get("camera") or {}
    if cam.get("path") not in _VALID_CAMERA:
        v.append(f"camera.path: unknown {cam.get('path')!r}")
    keys = cam.get("keys")
    if not isinstance(keys, list) or not keys:
        v.append("camera.keys: must be a non-empty list")
    else:
        prev = -1.0
        for i, k in enumerate(keys):
            if not isinstance(k, dict):
                v.append(f"camera.keys[{i}]: not an object")
                continue
            at = k.get("at")
            if not isinstance(at, (int, float)) or not (0.0 <= at <= 1.0):
                v.append(f"camera.keys[{i}].at must be 0..1, got {at!r}")
            elif at < prev:
                v.append(f"camera.keys[{i}].at ({at}) is out of order")
            else:
                prev = float(at)

    tex = spec.get("texture") or {}
    if tex.get("treatment") not in _VALID_TEXTURE:
        v.append(f"texture.treatment: unknown {tex.get('treatment')!r}")

    sig = spec.get("signature") or {}
    if sig.get("moment") not in _VALID_SIGNATURE:
        v.append(f"signature.moment: unknown {sig.get('moment')!r}")

    pal = spec.get("palette") or {}
    for key in ("bgBase", "bgAlt", "accent", "text", "muted"):
        if not isinstance(pal.get(key), str) or not pal.get(key):
            v.append(f"palette.{key}: missing")

    return v


def spec_summary(spec: dict[str, Any]) -> dict[str, Any]:
    """One-line human summary for logs / decision records."""
    return {
        "id": spec.get("id"),
        "scheme": spec.get("scheme"),
        "class": spec.get("contentClass"),
        "layers": len(spec.get("layers") or []),
        "camera": (spec.get("camera") or {}).get("path"),
        "signature": (spec.get("signature") or {}).get("moment"),
        "texture": (spec.get("texture") or {}).get("treatment"),
        "duration_s": spec.get("durationSeconds"),
    }
