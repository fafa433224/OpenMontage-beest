"""Tests for tools/integrations/figma_tokens.py — design tokens → playbook/brand.

Hermetic: uses a synthetic Tokens Studio JSON. The REST fetch path needs
a FIGMA_TOKEN and is covered by input-schema/contract checks only.
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.integrations.figma_tokens import (  # noqa: E402
    FigmaTokens,
    fetch_figma_tokens,
    tokens_to_brand,
    tokens_to_playbook,
)

TOKENS = {
    "color": {
        "brand": {
            "primary": {"$value": "#0E7C66", "$type": "color"},
            "accent": {"$value": "{color.brand.primary}"},
        },
        "bg": {"base": {"$value": "rgba(10, 20, 18, 1)", "$type": "color"}},
        "text": {"body": {"$value": "#EDF5F1", "$type": "color"}},
    },
    "font": {"family": {"$value": "Space Grotesk", "$type": "fontFamilies"}},
    "size": {
        "sm": {"$value": "14px", "$type": "dimension"},
        "md": {"$value": "18px"},
        "lg": {"$value": "32px"},
    },
}


@pytest.fixture(scope="module")
def tool():
    return FigmaTokens()


def test_tokens_to_playbook_shape():
    pb = tokens_to_playbook(TOKENS, "test-brand")
    for key in ("identity", "visual_language", "typography", "motion",
                "audio", "asset_generation", "quality_rules"):
        assert key in pb, f"missing {key}"
    pal = pb["visual_language"]["color_palette"]
    assert pal["primary"][0] == "#0E7C66"
    assert pal["text"] == "#EDF5F1"


def test_alias_resolution():
    pb = tokens_to_playbook(TOKENS, "alias")
    # accent aliases primary → same hex after resolution
    assert pb["visual_language"]["color_palette"]["accent"][0] == "#0E7C66"


def test_light_bg_gets_dark_canvas():
    light = json.loads(json.dumps(TOKENS))
    light["color"]["bg"]["base"]["$value"] = "#FFFFFF"
    pb = tokens_to_playbook(light, "light")
    assert pb["visual_language"]["color_palette"]["background"] == "#0B0E1A"


def test_tokens_to_brand_loads_via_brand_system(tmp_path):
    brand = tokens_to_brand(TOKENS, "tokenbrand")
    assert brand["palette"]["primary"] == "#0E7C66"
    assert brand["palette"]["family"] in (
        "indigo", "teal", "amber", "emerald", "magenta", "steel",
        "crimson", "violet", "sunset", "ocean",
    )
    # round-trip through the real loader (schema validation included)
    from lib.brand_profile import BrandProfile

    profile = BrandProfile.from_dict(brand, "tokenbrand")
    assert profile.primary == "#0E7C66"


def test_tool_import_writes_schema_valid_playbook(tool, tmp_path, monkeypatch):
    from styles import playbook_loader

    tokens_file = tmp_path / "tokens.json"
    tokens_file.write_text(json.dumps(TOKENS))

    custom = tmp_path / "styles-custom"
    monkeypatch.setattr(
        "tools.integrations.figma_tokens.STYLES_CUSTOM", custom
    )
    result = tool.execute({
        "action": "import",
        "tokens_path": str(tokens_file),
        "name": "smoke",
    })
    assert result.success, result.error
    written = custom / "smoke.yaml"
    assert written.exists()
    # the written playbook must validate via the repo's own loader
    import yaml

    loaded = yaml.safe_load(written.read_text())
    playbook_loader.validate_playbook(loaded)


def test_tool_import_missing_file_fails(tool, tmp_path):
    result = tool.execute({"action": "import", "tokens_path": str(tmp_path / "nope.json")})
    assert not result.success
    assert "not found" in result.error


def test_tool_fetch_needs_credentials(tool, monkeypatch):
    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    monkeypatch.delenv("FIGMA_FILE_KEY", raising=False)
    result = tool.execute({"action": "fetch"})
    assert not result.success
    assert "FIGMA_TOKEN" in result.error


def test_tool_mcp_hint(tool):
    result = tool.execute({"action": "mcp_hint"})
    assert result.success
    assert "3845" in result.data["hint"]  # Figma MCP default port


def test_tool_registered_in_registry():
    from tools.tool_registry import registry

    registry.discover()
    assert registry.get("figma_tokens") is not None


def test_figma_rest_parse_shape():
    """The REST parser logic is pure — feed it a synthetic Figma file doc."""
    class _Resp:
        def read(self):
            return json.dumps({
                "styles": {
                    "s1": {
                        "styleType": "FILL",
                        "description": "primary",
                        "nodes": [{"document": {"name": "Brand/Primary", "fills": [
                            {"type": "SOLID", "visible": True,
                             "color": {"r": 0.1, "g": 0.5, "b": 0.4}}
                        ]}}],
                    },
                    "s2": {
                        "styleType": "TEXT",
                        "description": "body",
                        "nodes": [{"document": {"name": "Body", "fills": [], "style": {
                            "fontFamily": "Inter", "fontSize": 16
                        }}}],
                    },
                }
            }).encode()

    class _Urlopen:
        def __enter__(self):
            return _Resp()

        def __exit__(self, *a):
            return False

    import urllib.request

    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: _Urlopen()
    try:
        tokens = fetch_figma_tokens("fake-token", "fake-key")
    finally:
        urllib.request.urlopen = orig

    assert tokens["color.primary"]["$value"] == "#1A8066"
    assert tokens["font.body"]["$value"] == "Inter"
