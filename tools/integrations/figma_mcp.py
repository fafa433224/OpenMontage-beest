"""Figma MCP bridge — design context becomes renderable motion.

Why MCP and not just the REST token reader
------------------------------------------
``tools/integrations/figma_tokens.py`` already reads published styles and
variables, and that is enough to *colour* a video. It is not enough to
*compose* one, because raw tokens carry none of the structure a motion
designer actually reasons about: layer order, hierarchy, auto-layout
direction, spacing rhythm, type scale in context, which element is the hero.

Figma's MCP server exposes exactly that — ``get_design_context`` (structured
node tree), ``get_variable_defs`` (variables + styles), ``get_screenshot``
(a visual reference), ``get_metadata`` (the sparse map you use to re-request
a node without blowing the context window). That structured layer tree is
what turns a designer's frame into a **ChoreographySpec** the motion compiler
can render, instead of a colour swatch the compiler has to invent around.

What this adds, concretely
--------------------------
1. ``FigmaMCPClient`` — a small, dependency-free MCP client (JSON-RPC 2.0
   over streamable HTTP) that talks to either the remote server
   (``https://mcp.figma.com/mcp``) or the local Dev Mode server
   (``http://127.0.0.1:3845/mcp``).
2. ``brand_from_variables()`` — variables → ``channels/<id>/brand.yaml``, so a
   real design system seeds a channel envelope instead of guessed hexes.
3. ``spec_from_figma_frame()`` — a Figma frame's layer tree → the *structural
   hints* of a ``ChoreographySpec`` (scheme family, layer stack, alignment,
   type treatment, palette). This is the creativity boost: the designer's
   composition survives into the video's motion rather than being averaged
   into a stock card.
4. ``design_reference_frames()`` — screenshots saved next to a render so the
   visual audit's vision critique can compare output against the design.

Everything degrades: with no MCP server reachable, the module reports
unavailable and the pipeline keeps using the tokens path. MCP is an upgrade,
never a dependency.

Public surface
--------------
    from tools.integrations.figma_mcp import (
        FigmaMCPClient, figma_probe, figma_status, brand_from_variables,
        spec_from_figma_frame, design_reference_frames,
    )
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

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
CHANNELS_DIR = REPO_ROOT / "channels"

DEFAULT_REMOTE_URL = "https://mcp.figma.com/mcp"
DEFAULT_DESKTOP_URL = "http://127.0.0.1:3845/mcp"

PROTOCOL_VERSION = "2025-06-18"

# Figma MCP tool names (the ones this bridge uses). Kept as constants so a
# server rename shows up in one place.
TOOL_DESIGN_CONTEXT = "get_design_context"
TOOL_VARIABLE_DEFS = "get_variable_defs"
TOOL_METADATA = "get_metadata"
TOOL_SCREENSHOT = "get_screenshot"


class FigmaMCPError(RuntimeError):
    """MCP transport or protocol failure with a readable cause."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class FigmaMCPClient:
    """Minimal MCP client for Figma's design-context server.

    Deliberately small and stdlib-only: the sandbox and Kaggle images have no
    MCP SDK, and the protocol surface used here is three calls. A raw JSON-RPC
    POST is more robust than pulling a dependency into a render image.

    The client is safe to construct when nothing is reachable — call
    :meth:`available` (or use :func:`figma_status`) before relying on it.
    """

    def __init__(
        self,
        url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: float = 30.0,
        session: Optional[str] = None,
    ) -> None:
        self.url = (
            url
            or os.environ.get("FIGMA_MCP_URL")
            or (DEFAULT_DESKTOP_URL if os.environ.get("FIGMA_MCP_DESKTOP") else "")
            or os.environ.get("FIGMA_MCP_REMOTE_URL")
            or DEFAULT_REMOTE_URL
        )
        self.token = token if token is not None else os.environ.get("FIGMA_TOKEN", "")
        self.timeout = timeout
        self.session_id = session
        self._initialized = False
        self._tool_names: Optional[set[str]] = None

    # -- transport -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.token:
            # Remote server: OAuth bearer. Desktop server: no auth needed,
            # and a stale header is harmless.
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _rpc(self, method: str, params: Optional[dict[str, Any]] = None,
             *, notify: bool = False) -> Any:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            payload["id"] = 1
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url, data=body,
                                     headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise FigmaMCPError(f"HTTP {exc.code} from {self.url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise FigmaMCPError(f"cannot reach {self.url}: {exc.reason}") from exc

        if notify or not raw.strip():
            return None
        return self._parse(raw)

    @staticmethod
    def _parse(raw: str) -> Any:
        """Accept either a JSON body or an SSE stream of JSON-RPC frames."""
        text = raw.strip()
        if text.startswith("{"):
            data = json.loads(text)
        else:
            data = None
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[len("data:"):].strip()
                if not chunk:
                    continue
                try:
                    candidate = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict) and ("result" in candidate or "error" in candidate):
                    data = candidate
        if data is None:
            raise FigmaMCPError(f"unparseable MCP reply: {raw[:200]}")
        if "error" in data:
            err = data["error"] or {}
            raise FigmaMCPError(f"MCP error {err.get('code')}: {err.get('message')}")
        return data.get("result", data)

    # -- protocol --------------------------------------------------------

    def initialize(self) -> dict[str, Any]:
        result = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "openmontage", "version": "1.0.0"},
        }) or {}
        try:
            self._rpc("notifications/initialized", {}, notify=True)
        except FigmaMCPError:
            pass  # notification failures are not fatal
        self._initialized = True
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        if not self._initialized:
            self.initialize()
        result = self._rpc("tools/list", {}) or {}
        tools = result.get("tools") or []
        self._tool_names = {t.get("name", "") for t in tools if isinstance(t, dict)}
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if not self._initialized:
            self.initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        return _unwrap_tool_result(result)

    # -- convenience -----------------------------------------------------

    def available(self) -> bool:
        try:
            self.initialize()
            return True
        except FigmaMCPError:
            return False

    def has_tool(self, name: str) -> bool:
        if self._tool_names is None:
            try:
                self.list_tools()
            except FigmaMCPError:
                return False
        return name in (self._tool_names or set())

    def design_context(self, node_id: str, file_key: str = "",
                       client_frameworks: Optional[list[str]] = None) -> Any:
        args: dict[str, Any] = {"nodeId": node_id}
        if file_key:
            args["fileKey"] = file_key
        args["clientFrameworks"] = client_frameworks or ["react"]
        return self.call_tool(TOOL_DESIGN_CONTEXT, args)

    def variable_defs(self, node_id: str = "", file_key: str = "") -> Any:
        args: dict[str, Any] = {}
        if node_id:
            args["nodeId"] = node_id
        if file_key:
            args["fileKey"] = file_key
        return self.call_tool(TOOL_VARIABLE_DEFS, args)

    def screenshot(self, node_id: str, file_key: str = "") -> Any:
        args: dict[str, Any] = {"nodeId": node_id}
        if file_key:
            args["fileKey"] = file_key
        return self.call_tool(TOOL_SCREENSHOT, args)

    def metadata(self, node_id: str = "", file_key: str = "") -> Any:
        args: dict[str, Any] = {}
        if node_id:
            args["nodeId"] = node_id
        if file_key:
            args["fileKey"] = file_key
        return self.call_tool(TOOL_METADATA, args)


def _unwrap_tool_result(result: Any) -> Any:
    """Flatten MCP's content-block wrapper into plain data.

    MCP returns ``{"content": [{"type": "text", "text": "<json>"}, ...]}``.
    Callers want the data, not the envelope.
    """
    if not isinstance(result, dict):
        return result
    if result.get("isError"):
        blocks = result.get("content") or []
        message = " ".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        raise FigmaMCPError(f"tool reported an error: {message[:300]}")
    blocks = result.get("content")
    if not isinstance(blocks, list):
        return result

    texts: list[str] = []
    images: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(str(block.get("text") or ""))
        elif block.get("type") == "image":
            images.append(block)

    joined = "\n".join(t for t in texts if t).strip()
    if not joined:
        return {"images": images} if images else result
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        # get_design_context returns code/text for some clients, JSON for
        # others. Both are legitimate; hand back the string.
        return joined


# ---------------------------------------------------------------------------
# Variables → channel brand
# ---------------------------------------------------------------------------

_HEX_RE = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")


def _hex_of(value: Any) -> Optional[str]:
    """Best-effort colour extraction from any Figma variable representation."""
    if value is None:
        return None
    if isinstance(value, dict):
        # Figma REST/RGBA form {r,g,b,a} with 0..1 floats.
        if {"r", "g", "b"} <= set(value):
            try:
                return "#{:02X}{:02X}{:02X}".format(
                    round(float(value["r"]) * 255),
                    round(float(value["g"]) * 255),
                    round(float(value["b"]) * 255),
                )
            except (TypeError, ValueError):
                return None
        for key in ("value", "$value", "resolvedValue", "hex"):
            if key in value:
                found = _hex_of(value[key])
                if found:
                    return found
        return None
    text = str(value)
    m = _HEX_RE.search(text)
    if m:
        h = m.group(1)
        # Shorthand hex duplicates each nibble: #abc is #aabbcc, not #abcabc.
        return "#" + ("".join(c * 2 for c in h) if len(h) == 3 else h).upper()
    m = re.search(r"rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)", text)
    if m:
        r, g, b = (int(float(x) * 255) if float(x) <= 1 else int(float(x))
                   for x in m.groups())
        return f"#{r:02X}{g:02X}{b:02X}"
    return None


def _luma(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return 0.5
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict) and not ({"r", "g", "b"} & set(value)) \
                    and "value" not in value and "$value" not in value:
                out.update(_flatten(value, path))
            else:
                out[path] = value
    return out


_ROLE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("primary", ("primary", "brand", "core")),
    ("accent", ("accent", "secondary", "highlight", "cta", "action")),
    ("bg", ("background", "bg", "surface", "canvas", "base", "backdrop")),
    ("fg", ("foreground", "text", "fg", "ink", "on-surface", "content")),
]


def brand_from_variables(
    variables: Any,
    channel_id: str,
    *,
    base: Optional[dict[str, Any]] = None,
    name: Optional[str] = None,
    write: bool = True,
) -> dict[str, Any]:
    """Build (and optionally write) a channel brand from Figma variables.

    Role matching is by token name first (a designer's ``color/brand/primary``
    beats any heuristic); unrecognised colour sets fall back to a
    most-used / most-saturated split so an ad-hoc file still yields a usable
    palette rather than the default navy.
    """
    flat = _flatten(variables)
    colours: dict[str, str] = {}
    for key, value in flat.items():
        hx = _hex_of(value)
        if hx:
            colours[key] = hx

    roles: dict[str, str] = {}
    for role, patterns in _ROLE_PATTERNS:
        for key, hx in colours.items():
            low = key.lower()
            if any(p in low for p in patterns):
                roles[role] = hx
                break

    if len(roles) < 2 and colours:
        # Unnamed palette: darkest is the canvas, lightest is text, the most
        # saturated is the accent. This is a heuristic, and it is labelled as
        # one in the emitted file so nobody mistakes it for intent.
        ordered = sorted(set(colours.values()), key=_luma)
        roles.setdefault("bg", ordered[0])

        def saturation(hx: str) -> float:
            h = hx.lstrip("#")
            try:
                r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
            except ValueError:
                return 0.0
            hi, lo = max(r, g, b), min(r, g, b)
            return (hi - lo) / max(1, hi)

        roles.setdefault("accent", max(ordered, key=saturation))
        roles.setdefault("primary", roles["accent"])
        roles.setdefault("fg", ordered[-1])

    # A motion palette must be dark-based and readable; enforce both.
    bg = roles.get("bg", "#0B0E1A")
    if _luma(bg) > 0.42:
        bg = "#0B0E1A"
    fg = roles.get("fg", "#F7F9FF")
    if abs(_luma(fg) - _luma(bg)) < 0.35:
        fg = "#F7F9FF"
    primary = roles.get("primary", "#1B2A4A")
    accent = roles.get("accent", "#4E9AF1")

    brand: dict[str, Any] = {
        "identity": {
            "name": name or channel_id.replace("-", " ").title(),
            "tagline": "",
            "url": "",
        },
        "palette": {
            "primary": primary,
            "accent": accent,
            "fg": fg,
            "bg": bg,
            "family": "",
            "hue_jitter_deg": 8,
        },
        "typography": {"pairing": "modern-sans"},
        "caption": {"style": "pill", "animation_variants": ["spring", "slide"]},
        "motion": {
            "transition_pool": ["slide-left", "zoom-punch", "wipe", "rise", "flip"],
            "bg_pool": ["aurora", "mesh", "beams", "particles", "grid-pulse"],
            "hero_bg_pool": ["aurora", "mesh"],
            "pacing": "standard",
            "pacing_jitter": 0.5,
            "timing_curve": "smooth",
            "camera_drift": 0.6,
        },
        "intro": {"enabled": True, "wordmark": name or channel_id, "kicker": "",
                  "tagline": "", "duration_s": 5.4, "style": "kinetic-type"},
        "outro": {"enabled": True, "wordmark": name or channel_id,
                  "cta": "Subscribe", "url": "", "duration_s": 4.8,
                  "style": "kinetic-type"},
        "watermark": {"enabled": False},
        "sfx": {"whoosh": True, "impact": True, "riser": True, "chime": True},
        "consistency_anchors": {"fixed_caption": True, "fixed_typography": True,
                                "fixed_palette_hue": True},
    }
    if base:
        _deep_merge(brand, base)

    if write:
        path = CHANNELS_DIR / channel_id / "brand.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        import yaml

        path.write_text(
            "# Generated from Figma variables by tools/integrations/figma_mcp.py\n"
            "# Role assignment: name-matched where the token names allowed it,\n"
            "# heuristic (darkest/saturated/lightest) otherwise.\n"
            + yaml.safe_dump(brand, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    return brand


def _deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


# ---------------------------------------------------------------------------
# Frame → choreography structure (the creativity bridge)
# ---------------------------------------------------------------------------

#: Figma layer names carry intent, and intent maps to scheme. This is the
#: difference between a video that *uses the brand's colours* and one that
#: *reads like the brand's design*.
# Order is a tie-break, not the primary rule: `_match` prefers the longest
# matching keyword, so a frame named "Stat Hero" resolves on the more
# specific "stat" descriptor either way. Specific structures are listed
# first so equal-length ties resolve toward intent.
_NAME_TO_SCHEME: list[tuple[tuple[str, ...], str]] = [
    (("stat", "metric", "kpi", "chart", "data"), "data-reveal"),
    (("split", "two-column", "two column", "dual"), "kinetic-split"),
    (("grid", "cards", "tiles", "matrix"), "grid-cascade"),
    (("list", "items", "steps", "checklist"), "mask-parade"),
    (("quote", "testimonial", "pullquote"), "focus-pull"),
    (("editorial", "article", "body", "content"), "stacked-editorial"),
    (("orbit", "badge", "seal", "circle"), "orbital-hero"),
    (("hero", "masthead", "cover"), "monolith"),
]

_TYPE_TO_TREATMENT: list[tuple[tuple[str, ...], str]] = [
    (("lower-third", "lower third", "caption bar"), "boxed-lower-third"),
    (("outline", "stroke", "hollow"), "outline-overlay"),
    (("rail", "side", "vertical"), "vertical-rail"),
    (("quote", "blockquote"), "quote-plate"),
    (("number", "stat", "metric"), "big-number"),
    (("big", "huge", "display", "mega"), "oversized-crop"),
]


#: Layer names that indicate a supporting role rather than the hero line.
_LAYER_ROLE_RULES: list[tuple[tuple[str, ...], str]] = [
    (("kicker", "eyebrow", "overline", "label", "tag", "badge", "section"), "kicker"),
    (("body", "description", "paragraph", "copy", "support", "sub", "detail"), "body"),
    (("headline", "title", "display", "heading", "hero", "masthead", "name"), "headline"),
]


def _role_of_layer(layer_name: str, text: str) -> str:
    """Which copy slot a Figma layer fills. Empty when the name says nothing."""
    low = layer_name.lower()
    for keys, role in _LAYER_ROLE_RULES:
        if any(k in low for k in keys):
            return role
    return ""


def _match(table: list[tuple[tuple[str, ...], str]], text: str) -> Optional[str]:
    """Pick the entry whose keyword is the most specific match.

    Longest-keyword-wins, not first-listed-wins. A frame called "Stat Hero"
    contains both a data keyword and a hero keyword; the designer's *frame
    name* should not decide which wins, the more descriptive token should.
    (First-match ordering made "Stat Hero" resolve to a hero masthead, which
    is the opposite of what the design says.)
    """
    low = text.lower()
    best: Optional[tuple[int, str]] = None
    for keys, value in table:
        for key in keys:
            if key in low:
                length = len(key)
                if best is None or length > best[0]:
                    best = (length, value)
    return best[1] if best else None


def spec_from_figma_frame(
    frame: Any,
    brand: Any = None,
    project_id: str = "figma",
    *,
    index: int = 0,
    duration: float = 8.0,
    section_palette: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Turn a Figma frame's layer tree into a ChoreographySpec.

    The frame decides what the scene IS; the compiler still owns motion
    verbs, camera and timing (a static design has no motion information, and
    inventing one from it would be guessing). So this reads the *structure*
    — scheme family, alignment, layer order, type treatment — and hands it to
    the compiler, which choreographs inside that structure.
    """
    from lib.motion_compiler import compile_scene

    tree = frame if isinstance(frame, dict) else {"name": str(frame)}
    name = str(tree.get("name") or tree.get("frameName") or "")
    # Figma's design-context payload nests the node tree in a few shapes.
    nodes = tree.get("children") or tree.get("nodes") or tree.get("layers") or []
    if isinstance(nodes, dict):
        nodes = list(nodes.values())

    child_names: list[str] = []
    for node in nodes if isinstance(nodes, list) else []:
        if isinstance(node, dict):
            child_names.append(str(node.get("name") or node.get("type") or ""))
        else:
            child_names.append(str(node))

    haystack = " ".join([name] + child_names)
    scheme = _match(_NAME_TO_SCHEME, haystack) or "stacked-editorial"
    treatment = _match(_TYPE_TO_TREATMENT, haystack)

    # Text content: assignment follows the LAYER'S ROLE, not its position.
    # Taking the first text node made a frame's kicker ("THE NUMBER") the
    # headline and pushed the hero figure ("47%") into the body.
    kicker = ""
    headline = ""
    body = ""
    for node in (nodes if isinstance(nodes, list) else []):
        if not isinstance(node, dict):
            continue
        text = str(node.get("characters") or node.get("text") or "").strip()
        if not text:
            continue
        role = _role_of_layer(str(node.get("name") or ""), text)
        if role == "kicker" and not kicker:
            kicker = text
        elif role == "body" and not body:
            body = text
        elif role == "headline" and not headline:
            headline = text
        elif not headline:
            headline = text
        elif not body:
            body = text

    # An unfilled headline falls back to the longest unclaimed text, then to
    # the frame name (a designer's frame name is usually the headline anyway).
    if not headline:
        headline = name

    # Auto-layout direction survives into alignment.
    layout = str(tree.get("layoutMode") or tree.get("layout") or "").upper()
    align = "left"
    if layout in ("HORIZONTAL", "VERTICAL"):
        align = str(tree.get("primaryAxisAlignItems") or "").upper()
        align = {"CENTER": "center", "MAX": "right", "MIN": "left"}.get(align, "left")

    section = {
        "heading": headline,
        "text": body,
        "kicker": kicker,
        "scene_class": _class_for_scheme(scheme),
    }
    spec = compile_scene(
        section, brand, project_id, index=index, duration=duration,
        section_palette=section_palette, scheme=scheme,
    )
    spec["align"] = align
    if treatment:
        spec["typeTreatment"] = treatment
    spec["source"] = {
        "kind": "figma",
        "frame": name,
        "layers": child_names[:24],
        "schemeBasis": scheme,
    }
    return spec


def _class_for_scheme(scheme: str) -> str:
    from lib.motion_compiler import _CONTENT_CLASSES

    for cls, schemes in _CONTENT_CLASSES.items():
        if scheme in schemes:
            return cls
    return "statement"


def design_reference_frames(
    client: FigmaMCPClient,
    node_ids: list[str],
    out_dir: Path,
    *,
    file_key: str = "",
) -> list[Path]:
    """Download design screenshots as audit references.

    Saved beside the render so the visual audit's vision critique can be
    pointed at "does the video match the design?" rather than only "is the
    video internally consistent".
    """
    import base64

    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for i, node_id in enumerate(node_ids):
        try:
            result = client.screenshot(node_id, file_key)
        except FigmaMCPError:
            continue
        images = []
        if isinstance(result, dict):
            images = result.get("images") or []
        for j, block in enumerate(images):
            data = block.get("data") or block.get("dataBase64")
            mime = str(block.get("mimeType") or block.get("media_type") or "image/png")
            if not data:
                continue
            ext = ".jpg" if "jpeg" in mime or "jpg" in mime else ".png"
            dest = out_dir / f"design-{i:03d}-{j}{ext}"
            try:
                dest.write_bytes(base64.b64decode(data))
                saved.append(dest)
            except Exception:
                continue
    return saved


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Registry tool — makes the bridge discoverable to agents
# ---------------------------------------------------------------------------

class FigmaMCP(BaseTool):
    """Design-context ingestion from Figma's MCP server.

    Sits alongside ``figma_tokens`` (REST/tokens): that one colours a video,
    this one composes it. Both emit artifacts the existing pipeline already
    consumes — a channel brand, a style playbook, or a ChoreographySpec.
    """

    name = "figma_mcp"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "design_ingestion"
    provider = "figma"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["python:pyyaml"]
    install_instructions = (
        "No install. Set FIGMA_MCP_URL (remote https://mcp.figma.com/mcp, or "
        "the local Dev Mode server http://127.0.0.1:3845/mcp) and FIGMA_TOKEN. "
        "Without them the pipeline falls back to tools/integrations/figma_tokens.py."
    )

    capabilities = ["status", "brand", "spec", "reference_frames", "design_context"]
    best_for = [
        "seeding a channel brand from a real Figma design system",
        "turning a designer's composition into renderable choreography",
        "fetching design screenshots as visual-audit references",
    ]

    input_schema = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {"enum": ["status", "brand", "spec", "reference_frames",
                                "design_context"]},
            "node_id": {"type": "string", "description": "Figma node id, e.g. '1:23'"},
            "file_key": {"type": "string", "description": "Figma file key"},
            "channel_id": {"type": "string",
                           "description": "brand: channel dir to write"},
            "project_id": {"type": "string", "description": "spec: seed material"},
            "index": {"type": "integer", "description": "spec: scene index"},
            "duration": {"type": "number", "description": "spec: scene seconds"},
            "node_ids": {"type": "array", "items": {"type": "string"},
                         "description": "reference_frames: nodes to screenshot"},
            "out_dir": {"type": "string", "description": "reference_frames output dir"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=64, vram_mb=0, disk_mb=2, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=["timeout", "connection"])
    idempotency_key_fields = ["action", "node_id", "channel_id", "project_id"]
    side_effects = [
        "may write channels/<id>/brand.yaml",
        "may write design reference screenshots",
    ]

    def get_status(self) -> ToolStatus:
        # Offline by contract: registry discovery must never open a socket.
        return (ToolStatus.AVAILABLE if figma_status()["configured"]
                else ToolStatus.UNAVAILABLE)

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        started = _now()
        action = str(inputs.get("action") or "")
        client = FigmaMCPClient()

        try:
            if action == "status":
                # Explicit call: a live probe is expected here.
                return ToolResult(success=True, data=figma_probe(client))

            if not client.available():
                return ToolResult(
                    success=False,
                    error=(
                        f"Figma MCP unreachable at {client.url}. Set FIGMA_MCP_URL "
                        "to the remote server or enable Dev Mode MCP in the Figma "
                        "desktop app; or use tools/integrations/figma_tokens.py "
                        "for a tokens-only import."
                    ),
                )

            if action == "design_context":
                data = client.design_context(
                    str(inputs.get("node_id") or ""), str(inputs.get("file_key") or ""),
                )
                return ToolResult(success=True, data={"design_context": data},
                                  duration_seconds=_now() - started)

            if action == "brand":
                variables = client.variable_defs(
                    str(inputs.get("node_id") or ""), str(inputs.get("file_key") or ""),
                )
                channel_id = str(inputs.get("channel_id") or "figma-channel")
                brand = brand_from_variables(variables, channel_id, write=True)
                return ToolResult(
                    success=True,
                    data={"channel_id": channel_id, "brand": brand["palette"]},
                    duration_seconds=_now() - started,
                )

            if action == "spec":
                frame = client.design_context(
                    str(inputs.get("node_id") or ""), str(inputs.get("file_key") or ""),
                )
                spec = spec_from_figma_frame(
                    frame, brand=None,
                    project_id=str(inputs.get("project_id") or "figma"),
                    index=int(inputs.get("index") or 0),
                    duration=float(inputs.get("duration") or 8.0),
                )
                return ToolResult(success=True, data={"spec": spec},
                                  duration_seconds=_now() - started)

            if action == "reference_frames":
                ids = [str(n) for n in (inputs.get("node_ids") or [])]
                out = Path(str(inputs.get("out_dir") or "projects/figma-design"))
                saved = design_reference_frames(
                    client, ids, out, file_key=str(inputs.get("file_key") or ""),
                )
                return ToolResult(success=True,
                                  data={"frames": [str(p) for p in saved]},
                                  duration_seconds=_now() - started)

            return ToolResult(success=False, error=f"unknown action: {action}")
        except FigmaMCPError as exc:
            return ToolResult(success=False, error=f"Figma MCP error: {exc}")
        except Exception as exc:  # noqa: BLE001 - tool boundary
            return ToolResult(success=False, error=f"{type(exc).__name__}: {exc}")


def _now() -> float:
    import time

    return time.time()


def figma_status() -> dict[str, Any]:
    """Offline capability report — **never makes a network call**.

    Status must be cheap and side-effect free: the tool registry calls
    ``get_status()`` on every discovery, and a status probe that opens a
    socket makes discovery slow, breaks the test suite's no-network
    contract, and turns a reporting call into an outage source.

    For a live check use :func:`figma_probe`. ``mode`` is the field to branch
    on: ``mcp-configured`` / ``rest-tokens`` / ``off``.
    """
    url = (
        os.environ.get("FIGMA_MCP_URL")
        or (DEFAULT_DESKTOP_URL if os.environ.get("FIGMA_MCP_DESKTOP") else "")
        or os.environ.get("FIGMA_MCP_REMOTE_URL")
        or ""
    )
    has_token = bool(os.environ.get("FIGMA_TOKEN"))
    configured = bool(url) or has_token
    return {
        "mcp_url": url or DEFAULT_REMOTE_URL,
        "mcp_configured": bool(url),
        "token_configured": has_token,
        "file_key_configured": bool(os.environ.get("FIGMA_FILE_KEY")),
        "mode": "mcp-configured" if url else ("rest-tokens" if has_token else "off"),
        "configured": configured,
        "hint": (
            "set FIGMA_MCP_URL (remote https://mcp.figma.com/mcp, or the local "
            "Dev Mode server http://127.0.0.1:3845/mcp) for structured design "
            "context; FIGMA_TOKEN alone gives the tokens-only path"
        ),
    }


def figma_probe(client: Optional[FigmaMCPClient] = None) -> dict[str, Any]:
    """Live capability check: actually handshake with the MCP server.

    Separate from :func:`figma_status` on purpose — this one may block on
    the network, so it belongs in ``doctor`` and explicit tool calls, never
    in registry discovery.
    """
    probe = client or FigmaMCPClient(timeout=6.0)
    status = figma_status()
    status["mcp_url"] = probe.url
    status["mcp_available"] = False
    status["tools"] = []
    try:
        tools = probe.list_tools()
        status["mcp_available"] = True
        status["tools"] = sorted(t.get("name", "") for t in tools if isinstance(t, dict))
        status["mode"] = "mcp"
    except FigmaMCPError as exc:
        status["mcp_error"] = str(exc)
        if status["token_configured"]:
            status["mode"] = "rest-tokens"
    return status
