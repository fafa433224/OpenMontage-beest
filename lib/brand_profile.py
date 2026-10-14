"""Channel brand profiles — per-channel identity with seeded variety.

Why this module exists
----------------------
The creative director gives every *project* a fresh look, which fixed the
"every video looks the same" bug — but it also means two videos from the
*same YouTube channel* share nothing. Real channels do the opposite:

    every video on channel A feels like channel A,
    channel B feels different from channel A,
    and no two videos on either channel are identical.

That is a **brand envelope with bounded drift**:

* **Anchors** (fixed per channel): palette hue family + primary/accent,
  caption pack, typography, the transition/bg vocabulary, pacing range,
  intro/outro wordmark + structure, SFX set, watermark.
* **Drift** (seeded per project): the walk order through the vocabulary,
  palette lightness/glow variants, caption animation variant, pacing pick
  inside the brand range, alignment order, hero fraction.

Seeding
-------
``project_seed = f"{channel_id}:{project_id}"``. Same channel + same
project → identical render (retry-safe, Kaggle-reproducible). Same
channel + different project → same anchors, different drift. Different
channels → different envelopes → different look, by construction.

Integration points
------------------
* ``CreativeDirector(seed, brief, brand=profile)`` — anchors the director's
  choices inside the envelope (``lib/creative_director.py``).
* ``scripts/kaggle/build_explainer_props.py`` — emits ``brandOverlay``
  props (wordmark, watermark, fonts, intro/outro config) consumed by the
  Remotion ``Explainer`` composition.
* ``render_voiceover_video.py`` / the harness — fills the parameterized
  HyperFrames intro/outro templates (``{{WORDMARK}}`` etc.) from the
  brand instead of the hard-coded wordmark.
* ``lib/hyperframes_style_bridge.py`` — brand CSS vars ride the same
  bridge the playbooks use.

Brand files
-----------
``channels/<channel-id>/brand.yaml``. Ship three presets (``default``,
``tech-deep``, ``story-loops``) plus a JSON Schema
(``schemas/brands/channel.schema.json``) so custom channels fail fast
with a readable error instead of rendering wrong.
"""
from __future__ import annotations

import hashlib
import random
from pathlib import Path
from typing import Any, Optional

import yaml

# Repo vocabulary imports — brand files are validated against the SAME
# pools the renderers consume, so a brand can never reference a style the
# Remotion/HyperFrames stacks don't implement.
from lib.creative_director import (  # noqa: F401
    BG_STYLES,
    CAPTION_STYLE_PACKS,
    HERO_BG_STYLES,
    PALETTE_FAMILIES,
    TRANSITION_STYLES,
    TYPE_TRANSITIONS,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANNELS_DIR = REPO_ROOT / "channels"
SCHEMA_PATH = REPO_ROOT / "schemas" / "brands" / "channel.schema.json"

_FONT_FAMILIES = {
    # Maps brand typography to the fonts bundled in remotion-composer/src/fonts
    # (plus Google-fonts fallbacks). Anything else falls back to the bundle.
    "modern-sans": {"heading": "Space Grotesk", "body": "Space Grotesk"},
    "editorial-serif": {"heading": "Playfair Display", "body": "Space Grotesk"},
    "mono-tech": {"heading": "Space Grotesk", "body": "Sarasa Mono SC"},
    "display-bold": {"heading": "Playfair Display", "body": "Playfair Display"},
}


class BrandError(ValueError):
    """Raised when a brand file is structurally invalid."""


def _validate_pool(name: str, values: list[str], allowed: set[str]) -> None:
    unknown = [v for v in values if v not in allowed]
    if unknown:
        raise BrandError(
            f"brand.{name} references unknown styles {unknown}; "
            f"allowed: {sorted(allowed)}"
        )


class BrandProfile:
    """A loaded, validated channel brand envelope."""

    def __init__(self, data: dict[str, Any], channel_id: str) -> None:
        self.raw = dict(data)
        self.channel_id = channel_id

        ident = data.get("identity") or {}
        self.name = str(ident.get("name") or channel_id)
        self.tagline = str(ident.get("tagline") or "")
        self.url = str(ident.get("url") or "")
        self.logo = str(ident.get("logo") or "")  # path under remotion public dir

        pal = data.get("palette") or {}
        self.primary = str(pal.get("primary") or "#1A1A2E")
        self.accent = str(pal.get("accent") or "#E94560")
        self.fg = str(pal.get("fg") or "#F7F9FF")
        self.bg = str(pal.get("bg") or "#0B0B14")
        family = str(pal.get("family") or "").lower()
        if family and family not in PALETTE_FAMILIES:
            raise BrandError(
                f"brand.palette.family '{family}' is not one of {sorted(PALETTE_FAMILIES)}"
            )
        self.family = family
        self.hue_jitter = float(pal.get("hue_jitter_deg") or 8.0)

        typo = data.get("typography") or {}
        pairing = str(typo.get("pairing") or "modern-sans")
        if pairing not in _FONT_FAMILIES:
            raise BrandError(
                f"brand.typography.pairing '{pairing}' is not one of {sorted(_FONT_FAMILIES)}"
            )
        self.font_pairing = pairing
        self.fonts = dict(_FONT_FAMILIES[pairing])

        cap = data.get("caption") or {}
        self.caption_style = str(cap.get("style") or "pill")
        if self.caption_style not in CAPTION_STYLE_PACKS:
            raise BrandError(
                f"brand.caption.style '{self.caption_style}' is not one of "
                f"{sorted(CAPTION_STYLE_PACKS)}"
            )
        self.caption_variants = [
            str(v) for v in (cap.get("animation_variants") or ["spring", "slide"])
        ]

        motion = data.get("motion") or {}
        self.transition_pool = [
            str(t) for t in (motion.get("transition_pool") or TRANSITION_STYLES)
        ]
        _validate_pool("motion.transition_pool", self.transition_pool, set(TRANSITION_STYLES))
        self.bg_pool = [str(b) for b in (motion.get("bg_pool") or BG_STYLES)]
        _validate_pool("motion.bg_pool", self.bg_pool, set(BG_STYLES))
        self.hero_bg_pool = [
            str(b) for b in (motion.get("hero_bg_pool") or HERO_BG_STYLES)
        ]
        _validate_pool("motion.hero_bg_pool", self.hero_bg_pool, set(HERO_BG_STYLES))
        pacing = str(motion.get("pacing") or "standard")
        if pacing not in ("calm", "standard", "energetic"):
            raise BrandError(f"brand.motion.pacing '{pacing}' must be calm|standard|energetic")
        self.pacing = pacing
        self.pacing_jitter = max(0.0, min(1.0, float(motion.get("pacing_jitter") or 0.5)))
        self.timing_curve = str(motion.get("timing_curve") or "smooth")
        if self.timing_curve not in ("snappy", "smooth", "fluid"):
            raise BrandError("brand.motion.timing_curve must be snappy|smooth|fluid")
        self.camera_drift = max(0.0, min(1.0, float(motion.get("camera_drift") or 0.6)))

        intro = data.get("intro") or {}
        outro = data.get("outro") or {}
        self.intro_enabled = bool(intro.get("enabled", True))
        self.outro_enabled = bool(outro.get("enabled", True))
        self.intro_config = {
            "wordmark": str(intro.get("wordmark") or self.name),
            "kicker": str(intro.get("kicker") or ""),
            "tagline": str(intro.get("tagline") or self.tagline),
            "duration_s": float(intro.get("duration_s") or 5.4),
            "style": str(intro.get("style") or "kinetic-type"),
        }
        self.outro_config = {
            "wordmark": str(outro.get("wordmark") or self.name),
            "cta": str(outro.get("cta") or ""),
            "url": str(outro.get("url") or self.url),
            "duration_s": float(outro.get("duration_s") or 4.8),
            "style": str(outro.get("style") or "kinetic-type"),
        }

        wm = data.get("watermark") or {}
        self.watermark = {
            "enabled": bool(wm.get("enabled", False)),
            "text": str(wm.get("text") or self.name),
            "logo": str(wm.get("logo") or self.logo),
            "position": str(wm.get("position") or "bottom-right"),
            "opacity": max(0.0, min(1.0, float(wm.get("opacity") or 0.55))),
        }

        sfx = data.get("sfx") or {}
        self.sfx = {
            "whoosh": bool(sfx.get("whoosh", True)),
            "impact": bool(sfx.get("impact", True)),
            "riser": bool(sfx.get("riser", True)),
            "chime": bool(sfx.get("chime", True)),
        }

        anchors = data.get("consistency_anchors") or {}
        self.anchors = {
            "fixed_caption": bool(anchors.get("fixed_caption", True)),
            "fixed_typography": bool(anchors.get("fixed_typography", True)),
            "fixed_palette_hue": bool(anchors.get("fixed_palette_hue", True)),
        }

    # -- construction -----------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, Any], channel_id: str) -> "BrandProfile":
        profile = cls(data, channel_id)
        cls._validate_against_schema(profile.raw)
        return profile

    @classmethod
    def load(
        cls, channel_id: str, channels_dir: Optional[Path] = None
    ) -> "BrandProfile":
        base = Path(channels_dir) if channels_dir else CHANNELS_DIR
        path = base / channel_id / "brand.yaml"
        if not path.exists():
            available = sorted(
                d.name for d in base.glob("*") if (d / "brand.yaml").exists()
            )
            raise BrandError(
                f"channel '{channel_id}' not found at {path}. "
                f"Available channels: {available}"
            )
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_dict(data, channel_id)

    @staticmethod
    def _validate_against_schema(data: dict[str, Any]) -> None:
        if not SCHEMA_PATH.exists():
            return  # schema optional at runtime, never fatal
        try:
            import jsonschema

            schema = yaml.safe_load(
                SCHEMA_PATH.read_text(encoding="utf-8")
            ) if SCHEMA_PATH.suffix in (".yaml", ".yml") else __import__(
                "json"
            ).loads(SCHEMA_PATH.read_text(encoding="utf-8"))
            jsonschema.validate(data, schema)
        except ImportError:
            return
        except Exception as exc:  # jsonschema.ValidationError
            raise BrandError(f"brand file failed schema validation: {exc}") from exc

    # -- seeds: channel identity + project drift ---------------------------

    def project_seed(self, project_id: str) -> str:
        """Seed material binding the channel identity to a project.

        sha256 inside CreativeDirector makes this stable across machines.
        """
        return f"{self.channel_id}:{project_id}"

    def project_rng(self, project_id: str) -> random.Random:
        digest = hashlib.sha256(
            self.project_seed(project_id).encode("utf-8")
        ).digest()
        return random.Random(int.from_bytes(digest[:4], "big"))

    # -- director bridge ----------------------------------------------------

    def director_overrides(self, project_id: str) -> dict[str, Any]:
        """Brief-shaped overrides that anchor the CreativeDirector.

        The director still does the seeded walking (drift); these values
        pin the envelopes (anchors). LLM briefs may override *mood words*
        but never the anchors when ``fixed_*`` is set.
        """
        rng = self.project_rng(project_id)
        pacing = self.pacing
        if self.pacing_jitter > 0:
            order = ["calm", "standard", "energetic"]
            idx = order.index(pacing) + rng.choice((-1, 0, 1))
            pacing = order[max(0, min(len(order) - 1, idx))]
        overrides: dict[str, Any] = {
            "caption_style": self.caption_style,
            "pacing": pacing,
            "brand": {
                "channel_id": self.channel_id,
                "name": self.name,
                "primary": self.primary,
                "accent": self.accent,
                "fg": self.fg,
                "bg": self.bg,
                "family": self.family,
                "hue_jitter": self.hue_jitter,
                # Anchors the CreativeDirector consumes directly — kept
                # INSIDE the brand payload because build_props forwards
                # only this sub-dict to CreativeDirector(brand=...).
                "caption_style": self.caption_style,
                # Raw brand pacing — the CreativeDirector applies the
                # single per-project jitter (this seed-bound rng), so the
                # contract "within one step of brand base" holds.
                "pacing": self.pacing,
                "pacing_jitter": self.pacing_jitter,
                "transition_pool": self.transition_pool,
                "bg_pool": self.bg_pool,
                "hero_bg_pool": self.hero_bg_pool,
                "timing_curve": self.timing_curve,
                "camera_drift": self.camera_drift,
                "caption_variants": self.caption_variants,
                "anchors": self.anchors,
            },
        }
        if self.family:
            overrides["palette_family"] = self.family
        return overrides

    def palette_kwargs(self) -> dict[str, str]:
        return {"primary": self.primary, "accent": self.accent}

    # -- Remotion theme bridge ----------------------------------------------

    def theme_config(self) -> dict[str, Any]:
        """Partial ThemeConfig for the Remotion stack (merged over the
        default by ``Root.resolveTheme``; missing keys keep defaults)."""
        # timing_curve → spring feel + transition duration
        springs = {
            "snappy": {"damping": 18, "stiffness": 170, "mass": 0.9},
            "smooth": {"damping": 20, "stiffness": 120, "mass": 1},
            "fluid": {"damping": 26, "stiffness": 90, "mass": 1.1},
        }
        return {
            "primaryColor": self.primary,
            "accentColor": self.accent,
            "textColor": self.fg,
            "backgroundColor": self.bg,
            "headingFont": self.fonts["heading"],
            "bodyFont": self.fonts["body"],
            "captionHighlightColor": self.accent,
            "chartColors": [self.accent, self.primary, self.fg],
            "springConfig": springs.get(self.timing_curve, springs["smooth"]),
            "transitionDuration": {"snappy": 0.32, "smooth": 0.4, "fluid": 0.55}[
                self.timing_curve
            ],
        }

    def brand_overlay_props(self) -> dict[str, Any]:
        """Props consumed by Explainer's optional BrandOverlay layer."""
        return {
            "channelId": self.channel_id,
            "name": self.name,
            "tagline": self.tagline,
            "primary": self.primary,
            "accent": self.accent,
            "fontHeading": self.fonts["heading"],
            "fontBody": self.fonts["body"],
            "watermark": dict(self.watermark),
            "logo": self.logo,
        }

    # -- HyperFrames bridge ---------------------------------------------------

    def css_vars(self) -> dict[str, str]:
        """CSS custom properties for HyperFrames workspaces (style bridge)."""
        return {
            "--color-bg": self.bg,
            "--color-fg": self.fg,
            "--color-primary": self.primary,
            "--color-accent": self.accent,
            "--font-heading": f"'{self.fonts['heading']}', sans-serif",
            "--font-body": f"'{self.fonts['body']}', sans-serif",
        }

    def fill_html_template(self, html: str, project_id: str = "") -> str:
        """Replace ``{{TOKENS}}`` in a HyperFrames template with brand values.

        Tokens: WORDMARK KICKER TAGLINE URL CTA BG FG PRIMARY ACCENT
        FONT_HEAD FONT_BODY. Unknown tokens are left untouched.
        """
        intro, outro = self.intro_config, self.outro_config
        tokens = {
            "WORDMARK": intro.get("wordmark") or self.name,
            "KICKER": intro.get("kicker") or "",
            "TAGLINE": intro.get("tagline") or self.tagline,
            "URL": outro.get("url") or self.url,
            "CTA": outro.get("cta") or "",
            "BG": self.bg,
            "FG": self.fg,
            "PRIMARY": self.primary,
            "ACCENT": self.accent,
            "FONT_HEAD": self.fonts["heading"],
            "FONT_BODY": self.fonts["body"],
        }
        out = html
        for token, value in tokens.items():
            out = out.replace("{{" + token + "}}", str(value))
        return out

    def hyperframes_intro_outro_config(self) -> dict[str, Any]:
        """Config for the Kaggle/Harness HyperFrames intro-outro stage."""
        return {
            "intro": {
                "enabled": self.intro_enabled,
                "template": "intro",
                "expected_duration_s": self.intro_config["duration_s"],
                **self.intro_config,
            },
            "outro": {
                "enabled": self.outro_enabled,
                "template": "outro",
                "expected_duration_s": self.outro_config["duration_s"],
                **self.outro_config,
            },
            "css_vars": self.css_vars(),
        }

    # -- reporting --------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "name": self.name,
            "palette": {
                "primary": self.primary,
                "accent": self.accent,
                "family": self.family or "(derived)",
            },
            "caption_style": self.caption_style,
            "font_pairing": self.font_pairing,
            "pacing": self.pacing,
            "transition_pool": self.transition_pool,
            "watermark": self.watermark["enabled"],
        }


def list_channels(channels_dir: Optional[Path] = None) -> list[str]:
    base = Path(channels_dir) if channels_dir else CHANNELS_DIR
    if not base.exists():
        return []
    return sorted(d.name for d in base.glob("*") if (d / "brand.yaml").exists())


def load_brand(channel_id: Optional[str]) -> BrandProfile:
    """Load a brand by id; ``None``/''/``default`` → the default channel.

    The default channel is intentionally neutral so unbranded runs keep
    today's behavior (modulo the seeded director).
    """
    cid = (channel_id or "default").strip()
    return BrandProfile.load(cid)
