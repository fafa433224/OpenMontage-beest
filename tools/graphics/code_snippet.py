"""Code snippet renderer for overlay images.

Generates styled code screenshots using Pygments for syntax
highlighting and Pillow for rendering. No external services required.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolStability,
    ToolStatus,
    ToolTier,
)


# Monospace families tried in order when the Pygments ImageFormatter needs a
# font. Pygments' default ("DejaVu Sans Mono") only exists on systems that
# ship the DejaVu fonts; minimal containers (Kaggle, slim Docker images,
# fresh CI runners) often do not have it, which used to make every render
# raise FontNotFound instead of falling back to whatever mono font exists.
_MONOSPACE_CANDIDATES = [
    "DejaVu Sans Mono",
    "JetBrains Mono",
    "Liberation Mono",
    "Noto Sans Mono",
    "Roboto Mono",
    "Ubuntu Mono",
    "Courier New",
    "Courier",
    "FreeMono",
]


def _fc_family_exists(family: str) -> bool:
    """True if fontconfig can resolve ``family`` — the exact mechanism
    Pygments' FontManager uses on POSIX (``fc-list "Family:style=Regular"
    file``), so a True here guarantees Pygments will find the font too."""
    try:
        proc = subprocess.run(
            ["fc-list", f"{family}:style=Regular", "file"],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    for line in proc.stdout.splitlines():
        if line.startswith(b"Fontconfig warning:"):
            continue
        if line.strip().strip(b":"):
            return True
    return False


def _resolve_monospace_family() -> str:
    """Return the first monospace family fontconfig can actually resolve.

    Falls back to fontconfig's own ``monospace`` alias, then to the Pygments
    default (which reproduces the historical behaviour on font-rich systems).
    Result is cached: fontconfig lookups are subprocess calls and this runs
    once per process.
    """
    resolved = getattr(_resolve_monospace_family, "_cached", None)
    if resolved is not None:
        return resolved

    for family in _MONOSPACE_CANDIDATES:
        if _fc_family_exists(family):
            _resolve_monospace_family._cached = family  # type: ignore[attr-defined]
            return family

    # Ask fontconfig for its preferred monospace alias (works even when the
    # family name differs from our candidate list).
    try:
        proc = subprocess.run(
            ["fc-match", "-f", "%{family}", "monospace"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            family = proc.stdout.strip().split(",")[0].strip()
            if family:
                _resolve_monospace_family._cached = family  # type: ignore[attr-defined]
                return family
    except (OSError, subprocess.SubprocessError):
        pass

    _resolve_monospace_family._cached = "DejaVu Sans Mono"  # type: ignore[attr-defined]
    return "DejaVu Sans Mono"


# Theme presets mapping to Pygments styles and background colors
THEMES = {
    "monokai": {
        "pygments_style": "monokai",
        "bg_color": "#272822",
        "text_color": "#f8f8f2",
        "border_color": "#3e3d32",
    },
    "github_dark": {
        "pygments_style": "github-dark",
        "bg_color": "#0d1117",
        "text_color": "#c9d1d9",
        "border_color": "#30363d",
    },
    "dracula": {
        "pygments_style": "dracula",
        "bg_color": "#282a36",
        "text_color": "#f8f8f2",
        "border_color": "#44475a",
    },
    "one_dark": {
        "pygments_style": "one-dark",
        "bg_color": "#282c34",
        "text_color": "#abb2bf",
        "border_color": "#3e4452",
    },
    "solarized_dark": {
        "pygments_style": "solarized-dark",
        "bg_color": "#002b36",
        "text_color": "#839496",
        "border_color": "#073642",
    },
    "light": {
        "pygments_style": "default",
        "bg_color": "#ffffff",
        "text_color": "#333333",
        "border_color": "#e1e4e8",
    },
}


class CodeSnippet(BaseTool):
    name = "code_snippet"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "graphics"
    provider = "pygments"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC

    dependencies = ["python:pygments", "python:PIL"]
    install_instructions = "pip install Pygments Pillow"
    agent_skills = []

    capabilities = [
        "render_code_image",
        "syntax_highlight",
        "themed_code_card",
    ]

    input_schema = {
        "type": "object",
        "required": ["code"],
        "properties": {
            "code": {"type": "string"},
            "language": {"type": "string", "default": "python"},
            "theme": {
                "type": "string",
                "enum": list(THEMES.keys()),
                "default": "monokai",
            },
            "font_size": {"type": "integer", "default": 20},
            "font": {
                "type": "string",
                "default": None,
                "description": "Monospace font family override; auto-detected when omitted",
            },
            "padding": {"type": "integer", "default": 40},
            "border_radius": {"type": "integer", "default": 12},
            "line_numbers": {"type": "boolean", "default": True},
            "title": {"type": "string", "description": "Optional title bar text"},
            "output_path": {"type": "string"},
            "width": {"type": "integer", "description": "Force specific width"},
        },
    }

    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=50)
    idempotency_key_fields = ["code", "language", "theme", "font_size"]
    side_effects = ["writes image to output_path"]
    user_visible_verification = [
        "Verify code is readable and syntax highlighting is correct",
    ]

    def get_status(self) -> ToolStatus:
        try:
            import pygments  # noqa: F401
            from PIL import Image  # noqa: F401
            return ToolStatus.AVAILABLE
        except ImportError:
            return ToolStatus.UNAVAILABLE

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            from PIL import Image, ImageDraw, ImageFont
            from pygments import highlight
            from pygments.lexers import get_lexer_by_name, guess_lexer
            from pygments.formatters import ImageFormatter
        except ImportError:
            return ToolResult(
                success=False,
                error="Pygments and Pillow required. Run: pip install Pygments Pillow",
            )

        start = time.time()

        code = inputs["code"]
        language = inputs.get("language", "python")
        theme_name = inputs.get("theme", "monokai")
        font_size = inputs.get("font_size", 20)
        padding = inputs.get("padding", 40)
        line_numbers = inputs.get("line_numbers", True)
        title = inputs.get("title")
        output_path = Path(inputs.get("output_path", "code_snippet.png"))

        theme = THEMES.get(theme_name, THEMES["monokai"])

        try:
            lexer = get_lexer_by_name(language)
        except Exception:
            lexer = guess_lexer(code)

        # Use Pygments ImageFormatter for rendering. font_name is resolved
        # against the fonts actually installed on this system so minimal
        # containers (Kaggle, slim images) do not hit FontNotFound.
        formatter = ImageFormatter(
            style=theme["pygments_style"],
            font_size=font_size,
            font_name=inputs.get("font") or _resolve_monospace_family(),
            line_numbers=line_numbers,
            image_pad=padding,
            line_number_bg=theme["bg_color"],
            line_number_fg="#6272a4",
        )

        # Render to bytes
        image_bytes = highlight(code, lexer, formatter)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(image_bytes)

        # Add title bar if requested
        if title:
            self._add_title_bar(output_path, title, theme, font_size)

        elapsed = time.time() - start
        img = Image.open(output_path)

        return ToolResult(
            success=True,
            data={
                "output": str(output_path),
                "language": language,
                "theme": theme_name,
                "width": img.width,
                "height": img.height,
                "line_count": code.count("\n") + 1,
            },
            artifacts=[str(output_path)],
            duration_seconds=round(elapsed, 2),
        )

    def _add_title_bar(
        self, image_path: Path, title: str, theme: dict, font_size: int
    ) -> None:
        """Add a title bar to the top of the code image."""
        from PIL import Image, ImageDraw, ImageFont

        img = Image.open(image_path)
        bar_height = font_size + 20

        new_img = Image.new("RGB", (img.width, img.height + bar_height), theme["bg_color"])

        # Draw title bar
        draw = ImageDraw.Draw(new_img)
        draw.rectangle(
            [(0, 0), (img.width, bar_height)],
            fill=theme["border_color"],
        )

        # Draw window dots
        dot_y = bar_height // 2
        for i, color in enumerate(["#ff5f56", "#ffbd2e", "#27c93f"]):
            draw.ellipse(
                [(15 + i * 22, dot_y - 6), (15 + i * 22 + 12, dot_y + 6)],
                fill=color,
            )

        # Draw title text
        try:
            font = ImageFont.truetype("arial.ttf", font_size - 4)
        except (IOError, OSError):
            font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), title, font=font)
        text_width = bbox[2] - bbox[0]
        text_x = (img.width - text_width) // 2
        draw.text((text_x, 8), title, fill=theme["text_color"], font=font)

        # Paste original image below title bar
        new_img.paste(img, (0, bar_height))
        new_img.save(image_path)

    @staticmethod
    def list_themes() -> dict[str, str]:
        return {name: f"Background: {t['bg_color']}" for name, t in THEMES.items()}
