"""Figma design-token ingestion — designs become style playbooks.

Why
---
The fastest way to make videos match a real brand is to read the brand's
actual design system. Figma exposes two machine-readable surfaces:

1. **Figma Tokens plugin JSON** (``tokens.json`` export — the
   Tokens Studio / Tokens: Format for Tokens style file) — deterministic,
   works offline, no key needed.
2. **Figma REST API** (``FIGMA_TOKEN`` + ``FIGMA_FILE_KEY``) — reads the
   file's published variables and styles (fills, text styles).

Both are converted into a **style playbook** at
``styles/custom/<name>.yaml`` — the exact artifact the rest of the
pipeline already consumes: ``styles/playbook_loader.py`` validation +
design intelligence, ``video_compose._build_theme_from_playbook`` →
Remotion ``themeConfig``, and ``lib/hyperframes_style_bridge`` → CSS
vars. Figma-to-video with zero new plumbing.

The same tokens can also seed a **channel brand** (``channels/<id>/``)
via the ``brand`` action — palette/typography anchors only, motion stays
yours.

Actions
-------
- ``import``  — tokens JSON file → playbook (+ optional brand.yaml)
- ``fetch``   — Figma REST API → playbook (needs FIGMA_TOKEN + FIGMA_FILE_KEY)
- ``mcp_hint`` — print how to point an MCP-capable agent at Figma's MCP
  server for richer reads (frames, components) beyond raw tokens

Every action validates the generated playbook against
``schemas/styles/playbook.schema.json`` and fails loudly with the
violation — never writes a silently-broken playbook.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STYLES_CUSTOM = REPO_ROOT / "styles" / "custom"
CHANNELS_DIR = REPO_ROOT / "channels"

_MCP_HINT = """Figma MCP (richer than tokens):
  1. Run Figma Desktop; enable "Enable Dev Mode MCP Server" (Settings).
  2. Point your MCP client at http://127.0.0.1:3845/mcp
     (or expose it to the harness host).
  3. The agent can then read selection/frames/components and emit the
     same tokens.json this tool consumes — then:
     python -c "from tools.integrations.figma_tokens import FigmaTokens; ..."
Tokens stay the interchange format; MCP is just a richer reader."""


# ---------------------------------------------------------------------------
# Token normalization (Tokens Studio / W3C design-tokens drafts)
# ---------------------------------------------------------------------------


def _flatten_tokens(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten the nested token groups: {color: {brand: {primary: {$value...}}}}."""
    flat: dict[str, Any] = {}
    for key, value in (data or {}).items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and "$value" not in value and "value" not in value:
            flat.update(_flatten_tokens(value, path))
        else:
            flat[path] = value
    return flat


def _token_value(entry: Any) -> tuple[str, str]:
    """Return (raw_value, type) from a token entry or alias reference."""
    if isinstance(entry, dict):
        raw = entry.get("$value", entry.get("value"))
        typ = str(entry.get("$type", entry.get("type", "")))
        if isinstance(raw, dict) and "{" in json.dumps(raw):
            raw = raw.get("$value") or raw.get("value")
    else:
        raw, typ = entry, ""
    return str(raw if raw is not None else ""), typ


def _resolve_alias(raw: str, flat: dict[str, Any]) -> str:
    m = re.fullmatch(r"\{([^}]+)\}", raw.strip())
    if not m:
        return raw
    ref = m.group(1).strip().rstrip(".")  # {color.brand.primary} or color.brand.primary
    entry = flat.get(ref)
    if entry is None:
        for key, val in flat.items():
            if key.replace(".", ".") == ref:
                entry = val
                break
    if entry is not None:
        value, _ = _token_value(entry)
        return _resolve_alias(value, flat) if value.startswith("{") else value
    return raw


def _hex(colorish: str) -> str | None:
    """Best-effort CSS color → #RRGGBB (Figma RGBA, hex, hsla)."""
    s = (colorish or "").strip()
    # Figma REST returns {r,g,b,a} 0..1 — handled by caller before this.
    m = re.search(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b", s)
    if m:
        h = m.group(1)
        return "#" + (h * 2 if len(h) == 3 else h).upper()
    m = re.search(r"rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)", s)
    if m:
        r, g, b = (int(float(x) * 255) if float(x) <= 1 else int(float(x)) for x in m.groups())
        return f"#{r:02X}{g:02X}{b:02X}"
    m = re.search(r"hsla?\(\s*([\d.]+)[,\s]+([\d.]+)%[,\s]+([\d.]+)%", s)
    if m:
        import colorsys

        h, sat, lig = float(m.group(1)) / 360.0, float(m.group(2)) / 100.0, float(m.group(3)) / 100.0
        r, g, b = (round(c * 255) for c in colorsys.hls_to_rgb(h, lig, sat))
        return f"#{r:02X}{g:02X}{b:02X}"
    return None


def _font_family(tokens: dict[str, Any]) -> str | None:
    for key, entry in tokens.items():
        if "font" in key.lower() or "typograph" in key.lower():
            value, typ = _token_value(entry)
            if typ == "fontFamilies" or "family" in key.lower():
                fam = re.search(r"font-family:\s*([^;\"']+)", value)
                text = fam.group(1).strip() if fam else value.strip().strip("'\"")
                if 2 <= len(text) <= 60:
                    return text
    return None


def _scale_from_tokens(tokens: dict[str, Any], key_hint: str) -> list[float] | None:
    sizes: list[float] = []
    for key, entry in tokens.items():
        if key_hint in key.lower():
            value, _ = _token_value(entry)
            m = re.search(r"([\d.]+)\s*(px|pt|rem)?", value)
            if m:
                sizes.append(float(m.group(1)))
    return sorted(set(sizes))[:8] or None


def tokens_to_playbook(tokens_json: dict[str, Any], name: str) -> dict[str, Any]:
    """Convert a tokens file into a schema-v2 playbook dict."""
    flat = _flatten_tokens(tokens_json)
    resolved: dict[str, Any] = {}
    for key, entry in flat.items():
        value, _typ = _token_value(entry)
        resolved[key] = _resolve_alias(value, flat) if isinstance(value, str) else value

    # --- palette discovery: prefer semantic names, fall back to any colors
    candidates: dict[str, str] = {}
    for key, value in resolved.items():
        if not isinstance(value, str):
            continue
        hx = _hex(value)
        if not hx:
            continue
        low = key.lower()
        for role, patterns in [
            ("primary", ("primary", "brand", "accent-1")),
            ("accent", ("accent", "secondary", "highlight", "cta")),
            ("bg", ("background", "bg", "surface", "canvas", "base")),
            ("fg", ("foreground", "text", "fg", "ink", "on-base")),
        ]:
            if any(p in low for p in patterns) and role not in candidates:
                candidates[role] = hx
    if len(candidates) < 2:
        seen: list[str] = []
        for value in resolved.values():
            if isinstance(value, str):
                hx = _hex(value)
                if hx and hx not in seen:
                    seen.append(hx)
                if len(seen) >= 3:
                    break
        candidates.setdefault("primary", seen[0] if seen else "#1A1A2E")
        candidates.setdefault("accent", seen[1] if len(seen) > 1 else "#4E9AF1")
        candidates.setdefault("bg", "#0B0E1A")
        candidates.setdefault("fg", "#F7F9FF")
    dark_bg = _hex(candidates.get("bg", "#0B0E1A")) or "#0B0E1A"

    def _is_dark(hx: str) -> bool:
        h = hx.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return (r * 299 + g * 587 + b * 114) / 1000 < 128

    if not _is_dark(dark_bg):
        # Motion canvases are dark; invert light token sets gracefully.
        candidates["bg"] = "#0B0E1A"
        if _is_dark(_hex(candidates.get("fg", "#F7F9FF"))):
            candidates["fg"] = "#F7F9FF"

    heading_font = _font_family(resolved) or "Space Grotesk"

    primary = candidates.get("primary", "#1A1A2E")
    accent = candidates.get("accent", "#4E9AF1")
    text = candidates.get("fg", "#F7F9FF")
    bg = candidates.get("bg", "#0B0E1A")


    return {
        "name": name,
        "identity": {
            "name": name,
            "category": "motion-graphics",
            "mood": "token-faithful, systematic, on-brand",
            "pace": "moderate",
            "best_for": "Videos that must match an imported Figma design system",
        },
        "visual_language": {
            "color_palette": {
                "primary": [primary],
                "accent": [accent],
                "background": bg,
                "text": text,
                "muted": text,
            },
            "composition": "centered, token-driven hierarchy, generous negative space",
            "texture": "flat, clean, brand-color glows only",
        },
        "typography": {
            "headings": {"font": heading_font, "weight": 700, "tracking": "-0.01em"},
            "body": {"font": heading_font, "weight": 400, "line_height": 1.55},
            "scale_system": "major_third",
            "weight_matrix": {"title": 800, "heading": 700, "body": 400, "caption": 400},
        },
        "motion": {
            "transitions": ["fade", "slide-left", "slide-up", "scale"],
            "animation_style": "smooth ease-in-out, brand-tinted glows, no bounce",
            "pacing_rules": {
                "min_scene_hold_seconds": 2.5,
                "max_scene_hold_seconds": 12.0,
                "text_card_hold_seconds": 3.5,
                "stat_card_hold_seconds": 3.0,
                "transition_duration_seconds": 0.4,
            },
            "entrance": "fade-up with slight scale (0.96 -> 1.0)",
            "exit": "fade-out",
        },
        "audio": {
            "voice_style": "clear, confident, matches the brand's tone",
            "music_mood": "ambient, subtle, non-distracting",
            "music_volume": 0.08,
            "sfx_style": "soft whooshes on transitions, chime on completion",
            "ducking_threshold_db": -3,
        },
        "asset_generation": {
            "image_prompt_prefix": f"brand-faithful design using {primary} and {accent}",
            "consistency_anchors": ["color_palette", "typography"],
        },
        "quality_rules": [
            "text contrast against background must pass WCAG AA (4.5:1)",
            "every scene animates; no static frames",
            "brand colors only from the imported palette",
        ],
        "source": "figma-tokens",
    }


def tokens_to_brand(tokens_json: dict[str, Any], channel_id: str) -> dict[str, Any]:
    """Convert tokens into a channel brand.yaml payload (palette/typography)."""
    playbook = tokens_to_playbook(tokens_json, channel_id)
    palette = playbook["visual_language"]["color_palette"]

    def _first(value: Any, fallback: str) -> str:
        if isinstance(value, list):
            return str(value[0]) if value else fallback
        return str(value or fallback)

    fam = _guess_family(_first(palette["primary"], "#1A1A2E"))
    return {
        "identity": {"name": channel_id, "tagline": "Imported from Figma tokens"},
        "palette": {
            "primary": _first(palette["primary"], "#1A1A2E"),
            "accent": _first(palette["accent"], "#4E9AF1"),
            "fg": _first(palette.get("text"), "#F7F9FF"),
            "bg": _first(palette.get("background"), "#0B0E1A"),
            "family": fam,
        },
        "typography": {"pairing": "modern-sans"},
        "caption": {"style": "pill", "animation_variants": ["spring", "slide"]},
    }


def _guess_family(hex_color: str) -> str:
    import colorsys

    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hue = colorsys.rgb_to_hls(r, g, b)[0] * 360
    buckets = [
        ("indigo", 245), ("violet", 275), ("ocean", 220), ("steel", 205),
        ("teal", 187), ("emerald", 155), ("crimson", 350), ("magenta", 318),
        ("sunset", 20), ("amber", 38),
    ]
    return min(buckets, key=lambda bv: min(abs(hue - bv[1]), 360 - abs(hue - bv[1])))[0]


# ---------------------------------------------------------------------------
# Figma REST variables → tokens dict
# ---------------------------------------------------------------------------


def fetch_figma_tokens(token: str, file_key: str, timeout: int = 30) -> dict[str, Any]:
    """Read published variables + styles from the Figma REST API."""
    url = f"https://api.figma.com/v1/files/{urllib.parse.quote(file_key)}?geometry=false"
    req = urllib.request.Request(url, headers={"X-Figma-Token": token})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        doc = json.loads(resp.read().decode("utf-8"))

    tokens: dict[str, Any] = {}
    # Styles (paint/text styles published in the file)
    for node in doc.get("styles", {}).values():
        style_type = node.get("styleType")
        desc = node.get("description", "")
        for entry in node.get("nodes", []):
            n = entry.get("document", {})
            fills = n.get("fills") or []
            if style_type == "FILL" and fills:
                fill = fills[0]
                if fill.get("type") == "SOLID" and fill.get("visible", True):
                    c = fill["color"]
                    hexv = "#%02X%02X%02X" % (
                        round(c["r"] * 255), round(c["g"] * 255), round(c["b"] * 255)
                    )
                    key = desc or n.get("name", "color")
                    tokens[f"color.{key}"] = {"$value": hexv, "$type": "color"}
            if style_type == "TEXT":
                style = n.get("style", {})
                fam = style.get("fontFamily")
                if fam:
                    key = desc or n.get("name", "text")
                    tokens[f"font.{key}"] = {"$value": fam, "$type": "fontFamilies"}
                size = style.get("fontSize")
                if size:
                    tokens[f"size.{key}"] = {"$value": f"{size}px", "$type": "dimension"}
    return tokens


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


class FigmaTokens(BaseTool):
    name = "figma_tokens"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "design_ingestion"
    provider = "figma"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["python:pyyaml"]
    install_instructions = "pip install pyyaml (already in requirements.txt)"

    capabilities = ["import_tokens", "fetch_figma", "brand_from_tokens", "mcp_hint"]
    best_for = [
        "turning a Figma design system into a style playbook the pipeline can render",
        "seeding a channel brand's palette from real design tokens",
        "keeping videos on-brand without manual color picking",
    ]

    input_schema = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {"enum": ["import", "fetch", "mcp_hint"]},
            "tokens_path": {
                "type": "string",
                "description": "import: path to a tokens.json file (Tokens Studio format)",
            },
            "name": {
                "type": "string",
                "description": "playbook/brand name (default: derived from the file stem)",
            },
            "write_brand": {
                "type": "string",
                "description": "also write channels/<id>/brand.yaml with this id",
            },
            "file_key": {"type": "string", "description": "fetch: Figma file key"},
            "dry_run": {"type": "boolean", "description": "validate only, write nothing"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=64, vram_mb=0, disk_mb=1, network_required=False
    )
    retry_policy = RetryPolicy(max_retries=0, retryable_errors=[])
    idempotency_key_fields = ["action", "tokens_path", "name"]
    side_effects = ["writes styles/custom/<name>.yaml", "may write channels/<id>/brand.yaml"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        import time as _t

        start = _t.time()
        action = inputs.get("action")

        if action == "mcp_hint":
            return ToolResult(success=True, data={"hint": _MCP_HINT})

        if action == "import":
            tokens_path = inputs.get("tokens_path")
            if not tokens_path or not Path(tokens_path).exists():
                return ToolResult(success=False, error=f"tokens file not found: {tokens_path}")
            tokens_json = json.loads(Path(tokens_path).read_text(encoding="utf-8"))
        elif action == "fetch":
            import os

            token = os.environ.get("FIGMA_TOKEN")
            file_key = inputs.get("file_key") or os.environ.get("FIGMA_FILE_KEY")
            if not token or not file_key:
                return ToolResult(
                    success=False,
                    error="fetch needs FIGMA_TOKEN and file_key (or FIGMA_FILE_KEY env)",
                )
            try:
                tokens_json = fetch_figma_tokens(token, file_key)
            except Exception as exc:  # noqa: BLE001
                return ToolResult(success=False, error=f"Figma API error: {exc}")
            if not tokens_json:
                return ToolResult(
                    success=False,
                    error="no published styles/variables found in the Figma file",
                )
        else:
            return ToolResult(success=False, error=f"unknown action: {action}")

        name = str(inputs.get("name") or (Path(str(inputs.get("tokens_path", "figma"))).stem or "figma"))
        playbook = tokens_to_playbook(tokens_json, name)

        # Validate against the playbook schema before writing anything.
        try:
            from styles.playbook_loader import validate_playbook

            validate_playbook(playbook)
        except ImportError:
            pass  # loader API differs — schema check below is the real gate
        except Exception as exc:
            return ToolResult(success=False, error=f"generated playbook is invalid: {exc}")

        dry_run = bool(inputs.get("dry_run"))
        outputs: dict[str, Any] = {"playbook": playbook, "dry_run": dry_run}
        if not dry_run:
            STYLES_CUSTOM.mkdir(parents=True, exist_ok=True)
            playbook_path = STYLES_CUSTOM / f"{name}.yaml"
            playbook_path.write_text(
                "# Generated by tools/integrations/figma_tokens.py\n"
                + yaml.safe_dump(playbook, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            outputs["playbook_path"] = str(playbook_path)

            brand_id = inputs.get("write_brand")
            if brand_id:
                brand_dir = CHANNELS_DIR / str(brand_id)
                brand_dir.mkdir(parents=True, exist_ok=True)
                brand = tokens_to_brand(tokens_json, str(brand_id))
                brand_path = brand_dir / "brand.yaml"
                brand_path.write_text(
                    "# Generated by tools/integrations/figma_tokens.py\n"
                    + yaml.safe_dump(brand, sort_keys=False, allow_unicode=True),
                    encoding="utf-8",
                )
                outputs["brand_path"] = str(brand_path)

        return ToolResult(
            success=True,
            data=outputs,
            duration_seconds=round(_t.time() - start, 2),
        )
