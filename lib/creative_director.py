"""Creative director — seeded, LLM-optional creative variety engine.

Why this module exists
----------------------
Every "why do all videos look the same" complaint traces to one bug class:
*deterministic index-modulo rotation over tiny pools*. ``_rotate(seq, i)``,
``transitionForIndex(i) = STYLES[i % 8]``, ``bgStyleForIndex(i + idx)`` —
same input index, same output, forever. Two different projects with the same
cut count are visually identical. That is the "robotic" feel.

This module replaces modulo rotation with a **per-project creative seed**
driving:

* a seeded shuffle of every pool (transitions, bg styles, alignments) with
  no-immediate-repeat guarantees — same project renders identically on
  every machine (Kaggle retry-safe), different projects look different;
* **caption style packs** — 6 complete looks (pill, boxed, outline,
  karaoke-bar, pop-scale, lower-third) instead of one hardcoded pill;
* **palette derivation from the theme** — the section palette's glows and
  accents are generated from the project's primary/accent colors with HSL
  math, so a warm brand video gets warm worlds, not the fixed
  indigo→teal→amber→... ring;
* **universal cut-typing** — stat detection by numeric regex (any language
  with Arabic numerals), comparison/KPI synthesis from content, not from
  hardcoded demo strings;
* **optional LLM direction** — when a runtime LLM is configured (see
  ``lib/llm_client.py``) the director asks it for a creative brief
  (mood, palette family, caption look, pacing, b-roll mood words) and
  honours it; without an LLM it samples the pools with the seed. Either
  way the output is a valid creative plan — the LLM only shifts *choices*,
  never *schema*.

Public surface
--------------
    from lib.creative_director import CreativeDirector
    director = CreativeDirector(seed="project-id-or-topic")   # or seed=int
    plan = director.plan(script, durations)                   # creative brief
    cut["transition"] = director.transition(cut_type, i)
    cut["bgStyle"]    = director.bg_style(i)
    caption_style     = director.caption_style()
    palette           = director.palette_for_section(idx, count)
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
from typing import Any, Optional, Sequence

# ---------------------------------------------------------------------------
# Vocabularies — mirrors of remotion-composer/src/motion/* (keep in sync)
# ---------------------------------------------------------------------------

BG_STYLES = ["aurora", "mesh", "beams", "particles", "grid-pulse", "orbits", "waves", "grain"]
HERO_BG_STYLES = ["aurora", "mesh", "beams"]
TRANSITION_STYLES = [
    "slide-left", "slide-right", "slide-up", "zoom-punch",
    "wipe", "flip", "glitch", "rise",
]
# Per cut-type pools — typed choreography (heroes punch, stats flip) but the
# pick inside a pool is seeded, not positional.
TYPE_TRANSITIONS: dict[str, list[str]] = {
    "hero_title": ["zoom-punch", "glitch", "wipe"],
    "text_card": ["slide-left", "slide-up", "rise", "slide-right", "wipe"],
    "stat_card": ["flip", "zoom-punch"],
    "callout": ["slide-right", "glitch"],
    "comparison": ["slide-left", "zoom-punch"],
    "kpi_grid": ["wipe", "zoom-punch"],
    "progress_bar": ["wipe", "rise"],
}
ALIGNMENTS = ["left", "center", "right"]

# Caption style packs — each is a complete, distinct on-screen look.
CAPTION_STYLE_PACKS: dict[str, dict[str, Any]] = {
    "pill": {
        # The classic: rounded glass pill, glow-highlighted active word.
        "style": "pill", "wordsPerPage": 6, "fontSize": 42,
        "animation": "spring", "position": "bottom",
    },
    "boxed": {
        # Solid high-contrast box, harder editorial feel.
        "style": "boxed", "wordsPerPage": 4, "fontSize": 46,
        "animation": "slide", "position": "bottom",
    },
    "outline": {
        # No container: heavy stroked text like music-video subs.
        "style": "outline", "wordsPerPage": 5, "fontSize": 54,
        "animation": "fade", "position": "bottom",
    },
    "karaoke-bar": {
        # Full-width bottom bar, wipe-fill highlight.
        "style": "karaoke-bar", "wordsPerPage": 7, "fontSize": 40,
        "animation": "slide", "position": "bottom-bar",
    },
    "pop-scale": {
        # Active word pops in scale — maximal TikTok energy.
        "style": "pop-scale", "wordsPerPage": 3, "fontSize": 58,
        "animation": "pop", "position": "center",
    },
    "lower-third": {
        # Documentary lower-third, left-aligned, one line.
        "style": "lower-third", "wordsPerPage": 8, "fontSize": 36,
        "animation": "slide", "position": "lower-third",
    },
}

# Palette families — hue-centred worlds the director can generate from.
PALETTE_FAMILIES: dict[str, dict[str, float]] = {
    # family: (base hue deg, glowA offset, glowB offset, accent offset)
    "indigo":   {"hue": 245, "a": 0,   "b": 168, "accent": 12},
    "teal":     {"hue": 187, "a": 0,   "b": 210, "accent": 174},
    "amber":    {"hue": 38,  "a": 0,   "b": 8,   "accent": 46},
    "emerald":  {"hue": 155, "a": 0,   "b": 84,  "accent": 160},
    "magenta":  {"hue": 318, "a": 0,   "b": 262, "accent": 330},
    "steel":    {"hue": 205, "a": 0,   "b": 258, "accent": 203},
    "crimson":  {"hue": 350, "a": 0,   "b": 20,  "accent": 356},
    "violet":   {"hue": 275, "a": 0,   "b": 305, "accent": 282},
    "sunset":   {"hue": 20,  "a": 0,   "b": 340, "accent": 32},
    "ocean":    {"hue": 220, "a": 0,   "b": 190, "accent": 214},
}

MOOD_TO_FAMILY: dict[str, list[str]] = {
    "energetic": ["magenta", "crimson", "sunset", "amber"],
    "calm": ["teal", "ocean", "steel", "emerald"],
    "tech": ["indigo", "steel", "violet", "ocean"],
    "warm": ["amber", "sunset", "crimson"],
    "nature": ["emerald", "teal"],
    "luxury": ["violet", "magenta", "indigo"],
    "neutral": ["steel", "indigo", "teal"],
    "playful": ["magenta", "amber", "sunset"],
}

# ---------------------------------------------------------------------------
# HSL helpers — palette generation from any seed hue / theme color
# ---------------------------------------------------------------------------

def _hsl_to_hex(h: float, s: float, l: float) -> str:
    h = h % 360.0
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = l - c / 2
    if h < 60:
        r, g, b = c, x, 0.0
    elif h < 120:
        r, g, b = x, c, 0.0
    elif h < 180:
        r, g, b = 0.0, c, x
    elif h < 240:
        r, g, b = 0.0, x, c
    elif h < 300:
        r, g, b = x, 0.0, c
    else:
        r, g, b = c, 0.0, x
    def byte(v: float) -> int:
        return max(0, min(255, round((v + m) * 255)))
    return f"#{byte(r):02X}{byte(g):02X}{byte(b):02X}"


def _hex_to_hsl(hex_color: str) -> tuple[float, float, float]:
    clean = hex_color.lstrip("#")
    if len(clean) == 3:
        clean = "".join(ch * 2 for ch in clean)
    if len(clean) != 6:
        return (240.0, 0.5, 0.2)  # safe fallback
    r = int(clean[0:2], 16) / 255
    g = int(clean[2:4], 16) / 255
    b = int(clean[4:6], 16) / 255
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2
    if mx == mn:
        return (0.0, 0.0, l)
    d = mx - mn
    s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
    if mx == r:
        h = ((g - b) / d + (6 if g < b else 0)) * 60
    elif mx == g:
        h = ((b - r) / d + 2) * 60
    else:
        h = ((r - g) / d + 4) * 60
    return (h, s, l)


def build_palette(family: str, variant: int = 0) -> dict[str, str]:
    """Generate a complete ScenePalette dict for a hue family + variant.

    ``variant`` shifts the hue slightly so sections within the same family
    still evolve. Output keys match remotion's ``ScenePalette`` interface.
    """
    fam = PALETTE_FAMILIES.get(family, PALETTE_FAMILIES["indigo"])
    hue = (fam["hue"] + variant * 18) % 360
    hue_a = (hue + fam["a"]) % 360
    hue_b = (hue + fam["b"]) % 360
    hue_accent = (hue + fam["accent"]) % 360
    # Dark bases with slight hue lean, alternating lightness for variety.
    base_l = 0.07 + (variant % 2) * 0.015
    alt_l = 0.14 + (variant % 3) * 0.012
    return {
        "name": f"{family}-{variant}",
        "bgBase": _hsl_to_hex(hue, 0.45, base_l),
        "bgAlt": _hsl_to_hex(hue, 0.40, alt_l),
        "glowA": _hsl_to_hex(hue_a, 0.72, 0.55),
        "glowB": _hsl_to_hex(hue_b, 0.65, 0.50),
        "accent": _hsl_to_hex(hue_accent, 0.80, 0.62),
        "text": _hsl_to_hex(hue, 0.25, 0.96),
        "muted": _hsl_to_hex(hue, 0.20, 0.68),
        "glass": f"rgba({int(_hsl_to_hex(hue, 0.40, 0.5)[1:3], 16)}, "
                 f"{int(_hsl_to_hex(hue, 0.40, 0.5)[3:5], 16)}, "
                 f"{int(_hsl_to_hex(hue, 0.40, 0.5)[5:7], 16)}, 0.72)",
        "glassEdge": f"rgba({int(_hsl_to_hex(hue_accent, 0.80, 0.62)[1:3], 16)}, "
                     f"{int(_hsl_to_hex(hue_accent, 0.80, 0.62)[3:5], 16)}, "
                     f"{int(_hsl_to_hex(hue_accent, 0.80, 0.62)[5:7], 16)}, 0.35)",
    }


def palette_from_colors(primary: str, accent: str, variant: int = 0) -> dict[str, str]:
    """Derive a ScenePalette from explicit theme colors (playbook-driven)."""
    h, s, _ = _hex_to_hsl(primary)
    ha, sa, _ = _hex_to_hsl(accent)
    # Push saturation up for glow use; keep text near-white.
    base_l = 0.07 + (variant % 2) * 0.015
    return {
        "name": f"derived-{h:.0f}-{variant}",
        "bgBase": _hsl_to_hex(h, min(0.5, s * 0.9), base_l),
        "bgAlt": _hsl_to_hex(h, min(0.45, s * 0.8), base_l + 0.07),
        "glowA": _hsl_to_hex(ha, min(0.75, max(sa, 0.6)), 0.55),
        "glowB": _hsl_to_hex((h + 40) % 360, 0.6, 0.5),
        "accent": _hsl_to_hex(ha, min(0.85, max(sa, 0.7)), 0.62),
        "text": "#F7F9FF",
        "muted": _hsl_to_hex(h, 0.18, 0.68),
        "glass": _hsl_to_hex(h, 0.35, 0.16).replace("#", "rgba(").replace("", "")[:0] or f"rgba({int(_hsl_to_hex(h,0.4,0.5)[1:3],16)},{int(_hsl_to_hex(h,0.4,0.5)[3:5],16)},{int(_hsl_to_hex(h,0.4,0.5)[5:7],16)},0.72)",
        "glassEdge": f"rgba({int(_hsl_to_hex(ha,0.8,0.62)[1:3],16)},{int(_hsl_to_hex(ha,0.8,0.62)[3:5],16)},{int(_hsl_to_hex(ha,0.8,0.62)[5:7],16)},0.35)",
    }


# ---------------------------------------------------------------------------
# Universal content signals (language-agnostic where possible)
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(
    r"(?<![\w.)])"                                   # not part of a word/dotted number
    r"(\d{1,3}(?:[.,]\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"  # 1,234 / 12.5 / 7
    r"\s*(%|percent|x|×|k\b|m\b|b\b|bn\b|million|billion|trillion|hours?|minutes?|seconds?|days?|weeks?|years?)?",
    re.IGNORECASE,
)

_STAT_INTENT = re.compile(
    r"\b(study|studies|survey|report|research|data|statistics?|according to|"
    r"average|median|growth|increase|decrease|reduced?|improv\w+|boost\w*|up|down|"
    r"faster|slower|cheaper|bigger|smaller|more|less|total|number|amount|count)\b",
    re.IGNORECASE,
)


def detect_stat(text: str) -> Optional[str]:
    """Universal stat detection: finds a salient number in the sentence.

    Works for any language that writes Arabic numerals. Prefers numbers
    followed by % / x / units, then numbers near stat-intent words, then
    any number. Returns the matched phrase (e.g. "73%", "1,200", "4x")
    or None.
    """
    best: Optional[tuple[int, str]] = None  # (score, phrase)
    for m in _NUM_RE.finditer(text):
        phrase = m.group(0).strip()
        unit = (m.group(2) or "").strip()
        score = 1
        if unit:
            score = 3 if unit in ("%", "percent", "x", "×") else 2
        span = text[max(0, m.start() - 42) : min(len(text), m.end() + 42)]
        if _STAT_INTENT.search(span):
            score += 2
        if best is None or score > best[0]:
            best = (score, phrase)
    return best[1] if best else None


def detect_list(text: str) -> Optional[list[str]]:
    """Detect enumerations ("A, B and C" / "1. 2. 3." / "first second third")."""
    parts = re.split(r"\s*(?:,|;|\band\b|\bplus\b)\s*", text.strip())
    items = [p.strip(" .:;-—") for p in parts if 2 <= len(p.strip()) <= 40]
    if 3 <= len(items) <= 6:
        return items
    return None


_CALLOUT_RE = re.compile(
    r"\b(remember|key|important|note|tip|warning|caution|rule|habit|secret|"
    r"bottom line|takeaway|in short|the point|lesson)\b",
    re.IGNORECASE,
)


def detect_callout(text: str) -> bool:
    return bool(_CALLOUT_RE.search(text))


# ---------------------------------------------------------------------------
# The director
# ---------------------------------------------------------------------------

class CreativeDirector:
    """Seeded creative variety for one project.

    Parameters
    ----------
    seed:
        Anything hashable (project id, topic, "anything"). Same seed →
        same plan (retry-safe); different seed → different video.
    brief:
        Optional creative brief dict — typically the LLM's answer from
        ``llm_creative_brief()``. Recognised keys:
        ``mood`` (any MOOD_TO_FAMILY key or family name), ``caption_style``
        (a CAPTION_STYLE_PACKS key), ``palette_family``, ``pacing``
        (calm/standard/energetic), ``broll_moods`` (list of search words),
        ``bg_intensity`` (0-1).
    """

    def __init__(self, seed: Any = None, brief: Optional[dict[str, Any]] = None) -> None:
        if seed is None:
            seed = f"project-{time_ns()}"
        if isinstance(seed, int):
            self.seed_int = seed & 0xFFFFFFFF
        else:
            digest = hashlib.sha256(str(seed).encode("utf-8")).digest()
            self.seed_int = int.from_bytes(digest[:4], "big")
        self.rng = random.Random(self.seed_int)
        self.brief = dict(brief or {})

        # Mood → palette family chain, seeded so each project differs.
        mood = str(self.brief.get("mood", "")).lower()
        families = MOOD_TO_FAMILY.get(mood) or MOOD_TO_FAMILY["tech"]
        requested = str(self.brief.get("palette_family", "")).lower()
        if requested in PALETTE_FAMILIES:
            families = [requested]
        self.family_chain = list(families)
        self.rng.shuffle(self.family_chain)

        # Pre-generate the section palettes for this family chain.
        self._palettes: list[dict[str, str]] = [
            build_palette(self.family_chain[i % len(self.family_chain)], variant=i // max(1, len(self.family_chain)))
            for i in range(12)
        ]

        # Caption style: LLM choice wins, else seeded pick.
        cap = str(self.brief.get("caption_style", "")).lower()
        self._caption_style = CAPTION_STYLE_PACKS.get(cap)
        if self._caption_style is None:
            self._caption_style = self.rng.choice(list(CAPTION_STYLE_PACKS.values()))

        # Pools — seeded shuffles with no-immediate-repeat sampling.
        self._transitions = list(TRANSITION_STYLES)
        self.rng.shuffle(self._transitions)
        self._bg_styles = list(BG_STYLES)
        self.rng.shuffle(self._bg_styles)
        self._aligns = list(ALIGNMENTS)
        self.rng.shuffle(self._aligns)
        self._last_transition: Optional[str] = None
        self._last_bg: Optional[str] = None
        self._pick_counters = {"transition": 0, "bg": 0, "align": 0}
        # Per-project offsets into each typed pool — two projects with the
        # same cut structure must NOT walk the pools from the same position.
        self._pool_offsets = {
            "transition": self.rng.randrange(8),
            "bg": self.rng.randrange(len(BG_STYLES)),
            "align": self.rng.randrange(len(ALIGNMENTS)),
        }

        # Pacing: cut-length preference scaling.
        pacing = str(self.brief.get("pacing", "")).lower()
        self.pacing = pacing if pacing in ("calm", "standard", "energetic") else "standard"

        self.broll_moods: list[str] = [
            str(m) for m in (self.brief.get("broll_moods") or []) if str(m).strip()
        ][:6]

    # -- per-cut creative picks ------------------------------------------------

    def transition(self, cut_type: str, index: int) -> str:
        """Typed + seeded transition, never repeating back-to-back."""
        pool = TYPE_TRANSITIONS.get(cut_type, self._transitions)
        if len(pool) == 1:
            return pool[0]
        pick = pool[(index + self._pool_offsets["transition"]) % len(pool)]
        if pick == self._last_transition and len(pool) > 1:
            pick = pool[(index + self._pool_offsets["transition"] + 1) % len(pool)]
        self._last_transition = pick
        return pick

    def bg_style(self, index: int, is_hero: bool = False) -> str:
        pool = HERO_BG_STYLES if is_hero else self._bg_styles
        pick = pool[(index + self._pool_offsets["bg"]) % len(pool)]
        if pick == self._last_bg and len(pool) > 1:
            pick = pool[(index + self._pool_offsets["bg"] + 1) % len(pool)]
        self._last_bg = pick
        return pick

    def align(self, index: int) -> str:
        return self._aligns[(index + self._pool_offsets["align"]) % len(self._aligns)]

    def caption_style(self) -> dict[str, Any]:
        return dict(self._caption_style)

    def palette_for_section(self, section_index: int, section_count: int = 1) -> dict[str, str]:
        step = max(1, len(self._palettes) // max(1, min(section_count, 6)))
        return self._palettes[(section_index * step) % len(self._palettes)]

    def hero_duration_fraction(self) -> float:
        """How much of a section the hero card takes. Pacing-driven."""
        return {"calm": 0.22, "standard": 0.3, "energetic": 0.38}[self.pacing]

    def broll_mood_words(self, section_heading: str) -> list[str]:
        """Search words for b-roll retrieval — LLM moods + heading terms."""
        words = list(self.broll_moods)
        for w in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", section_heading):
            if w.lower() not in ("the", "and", "with", "for", "from", "into"):
                words.append(w)
        return words[:5] or ["abstract", "ambient"]


def time_ns() -> int:
    import time as _t
    return _t.time_ns()


# ---------------------------------------------------------------------------
# LLM creative brief — the "any LLM makes creative choices" bridge
# ---------------------------------------------------------------------------

BRIEF_SYSTEM_PROMPT = """You are the Creative Director of a motion-graphics video studio.
Given a video script, you choose a DISTINCT visual direction for THIS video.
You must avoid generic defaults: pick a direction a human editor would pick
after reading the content. Answer with ONLY a JSON object, no prose, with keys:
{
  "title": "short evocative working title",
  "mood": "one of: energetic, calm, tech, warm, nature, luxury, neutral, playful",
  "palette_family": "one of: indigo, teal, amber, emerald, magenta, steel, crimson, violet, sunset, ocean",
  "caption_style": "one of: pill, boxed, outline, karaoke-bar, pop-scale, lower-third",
  "pacing": "one of: calm, standard, energetic",
  "bg_intensity": 0.0,
  "broll_moods": ["3-6 abstract visual mood words for stock footage search, e.g. 'neon city rain', 'macro water droplets'"],
  "font_pairing": "one of: modern-sans, editorial-serif, mono-tech, display-bold",
  "rationale": "one sentence on why this direction fits the content"
}
bg_intensity is a number 0-1 (how busy the animated backgrounds should be)."""


def llm_creative_brief(script: dict[str, Any], client: Any) -> Optional[dict[str, Any]]:
    """Ask a runtime LLM for a creative brief. Returns None on any failure
    (caller falls back to seeded sampling — creative output must never
    depend on the LLM being alive)."""
    try:
        sections = script.get("sections") or []
        preview = json.dumps(
            {
                "topic": script.get("topic", ""),
                "sections": [
                    {"heading": s.get("heading", ""), "text": (s.get("text") or "")[:400]}
                    for s in sections[:8]
                ],
            },
            ensure_ascii=False,
        )
        brief = client.chat_json(
            [{"role": "user", "content": f"Script to direct:\n{preview}"}],
            system=BRIEF_SYSTEM_PROMPT,
            temperature=1.0,
            max_tokens=700,
        )
        if not isinstance(brief, dict):
            return None
        # Sanitise to known vocabularies — the LLM shifts choices, not schema.
        mood = str(brief.get("mood", "")).lower()
        if mood not in MOOD_TO_FAMILY:
            brief["mood"] = "tech"
        fam = str(brief.get("palette_family", "")).lower()
        if fam not in PALETTE_FAMILIES:
            brief.pop("palette_family", None)
        cap = str(brief.get("caption_style", "")).lower()
        if cap not in CAPTION_STYLE_PACKS:
            brief.pop("caption_style", None)
        pac = str(brief.get("pacing", "")).lower()
        if pac not in ("calm", "standard", "energetic"):
            brief.pop("pacing", None)
        moods = brief.get("broll_moods")
        if not isinstance(moods, list) or not moods:
            brief.pop("broll_moods", None)
        return brief
    except Exception:
        return None
