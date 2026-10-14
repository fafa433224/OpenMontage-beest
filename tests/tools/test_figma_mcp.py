"""Figma MCP bridge — token mapping, frame→spec, and graceful degradation.

No network is used: the client is exercised against a stub HTTP layer, and
the converters (which carry the real logic) are pure functions.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.integrations.figma_mcp import (  # noqa: E402
    FigmaMCPClient,
    FigmaMCPError,
    _flatten,
    _hex_of,
    _unwrap_tool_result,
    brand_from_variables,
    spec_from_figma_frame,
)


# ---------------------------------------------------------------------------
# Transport helpers
# ---------------------------------------------------------------------------

def test_hex_of_handles_rgba_dict():
    assert _hex_of({"r": 0.0, "g": 0.0, "b": 1.0, "a": 1.0}) == "#0000FF"


def test_hex_of_handles_nested_value_and_css():
    assert _hex_of({"$value": {"r": 1.0, "g": 0.0, "b": 0.0}}) == "#FF0000"
    assert _hex_of("rgb(255, 0, 0)") == "#FF0000"
    assert _hex_of("rgba(1, 0, 0, 0.5)") == "#FF0000"
    assert _hex_of("#abc") == "#AABBCC"
    assert _hex_of(None) is None


def test_flatten_walks_token_groups():
    flat = _flatten({"color": {"brand": {"primary": {"value": "#123456"}}}})
    assert flat == {"color.brand.primary": {"value": "#123456"}}


def test_unwrap_tool_result_parses_json_text_block():
    payload = {"content": [{"type": "text", "text": '{"name": "Frame"}'}]}
    assert _unwrap_tool_result(payload) == {"name": "Frame"}


def test_unwrap_tool_result_returns_plain_code_text():
    payload = {"content": [{"type": "text", "text": "export const Button = ..."}]}
    assert _unwrap_tool_result(payload) == "export const Button = ..."


def test_unwrap_tool_result_raises_on_is_error():
    with pytest.raises(FigmaMCPError, match="tool reported an error"):
        _unwrap_tool_result(
            {"isError": True, "content": [{"type": "text", "text": "bad node id"}]}
        )


def test_parse_accepts_sse_frames():
    raw = 'event: message\ndata: {"result": {"tools": []}}\n\n'
    assert FigmaMCPClient._parse(raw) == {"tools": []}


def test_parse_raises_on_garbage():
    with pytest.raises(FigmaMCPError):
        FigmaMCPClient._parse("not json at all")


def test_parse_raises_on_rpc_error():
    with pytest.raises(FigmaMCPError, match="MCP error"):
        FigmaMCPClient._parse('{"error": {"code": -32601, "message": "no such method"}}')


def test_client_unavailable_when_nothing_reachable():
    # A closed port on localhost: connection refused, fast, no network egress.
    client = FigmaMCPClient(url="http://127.0.0.1:9/mcp", timeout=1.0)
    assert client.available() is False


# ---------------------------------------------------------------------------
# Variables → brand
# ---------------------------------------------------------------------------

def test_brand_from_named_variables_uses_the_names(tmp_path, monkeypatch):
    import tools.integrations.figma_mcp as mod

    monkeypatch.setattr(mod, "CHANNELS_DIR", tmp_path)
    variables = {
        "color/brand/primary": {"value": "#1122AA"},
        "color/accent/cta": {"value": "#FF6600"},
        "color/background/base": {"value": "#0A0A12"},
        "color/text/ink": {"value": "#F2F4FF"},
    }
    brand = brand_from_variables(variables, "from-figma", write=True)
    pal = brand["palette"]
    assert pal["primary"] == "#1122AA"
    assert pal["accent"] == "#FF6600"
    assert pal["bg"] == "#0A0A12"
    assert pal["fg"] == "#F2F4FF"
    assert (tmp_path / "from-figma" / "brand.yaml").exists()


def test_brand_from_unnamed_variables_falls_back_to_heuristics(tmp_path, monkeypatch):
    import tools.integrations.figma_mcp as mod

    monkeypatch.setattr(mod, "CHANNELS_DIR", tmp_path)
    variables = {"a": "#101010", "b": "#F5F5F5", "c": "#E01020"}
    brand = brand_from_variables(variables, "heuristic", write=False)
    pal = brand["palette"]
    assert pal["bg"] == "#101010"      # darkest becomes the canvas
    assert pal["fg"] == "#F5F5F5"      # lightest becomes the text
    assert pal["accent"] == "#E01020"  # most saturated becomes the accent


def test_brand_forces_dark_canvas_for_motion(tmp_path, monkeypatch):
    import tools.integrations.figma_mcp as mod

    monkeypatch.setattr(mod, "CHANNELS_DIR", tmp_path)
    brand = brand_from_variables(
        {"color/background/base": {"value": "#FFFFFF"}}, "light-brand", write=False,
    )
    assert brand["palette"]["bg"] == "#0B0E1A"


def test_brand_generated_file_validates_against_the_brand_loader(tmp_path, monkeypatch):
    """The generated brand must load through the real BrandProfile."""
    import tools.integrations.figma_mcp as mod
    from lib.brand_profile import BrandProfile

    monkeypatch.setattr(mod, "CHANNELS_DIR", tmp_path)
    brand_from_variables(
        {"color/brand/primary": {"value": "#203060"},
         "color/accent/cta": {"value": "#40C0FF"}},
        "figma-channel", write=True,
    )
    profile = BrandProfile.load("figma-channel", channels_dir=tmp_path)
    assert profile.accent == "#40C0FF"
    assert profile.primary == "#203060"


def test_brand_base_overrides_are_deep_merged(tmp_path, monkeypatch):
    import tools.integrations.figma_mcp as mod

    monkeypatch.setattr(mod, "CHANNELS_DIR", tmp_path)
    brand = brand_from_variables(
        {"color/brand/primary": {"value": "#112233"}}, "merged",
        base={"identity": {"name": "My Channel"}, "motion": {"pacing": "energetic"}},
        write=False,
    )
    assert brand["identity"]["name"] == "My Channel"
    assert brand["motion"]["pacing"] == "energetic"
    assert brand["motion"]["timing_curve"] == "smooth"  # untouched default kept


# ---------------------------------------------------------------------------
# Frame → spec (the creativity bridge)
# ---------------------------------------------------------------------------

FRAME = {
    "name": "Stat Hero",
    "layoutMode": "VERTICAL",
    "primaryAxisAlignItems": "CENTER",
    "children": [
        {"name": "Kicker", "characters": "THE NUMBER"},
        {"name": "Big Number", "characters": "47%"},
        {"name": "Body", "characters": "of runs fail before the first frame."},
    ],
}


def test_spec_from_figma_frame_picks_a_matching_scheme():
    spec = spec_from_figma_frame(FRAME, brand=None, project_id="fig", index=0)
    assert spec["scheme"] == "data-reveal"
    assert spec["text"]["headline"] == "47%"
    assert spec["align"] == "center"


def test_spec_from_figma_frame_derives_type_treatment():
    frame = {"name": "Huge Display Type", "children": [{"name": "Headline",
                                                       "characters": "HI"}]}
    spec = spec_from_figma_frame(frame, brand=None, project_id="fig", index=0)
    assert spec["typeTreatment"] == "oversized-crop"


def test_spec_from_figma_frame_prefers_the_most_specific_keyword():
    """A frame named "Stat Hero" is data-shaped, not a masthead."""
    spec = spec_from_figma_frame({"name": "Stat Hero"}, brand=None,
                                 project_id="fig", index=0)
    assert spec["scheme"] == "data-reveal"


def test_spec_from_figma_frame_records_provenance():
    spec = spec_from_figma_frame(FRAME, brand=None, project_id="fig", index=2)
    assert spec["source"]["kind"] == "figma"
    assert spec["source"]["frame"] == "Stat Hero"
    assert "Big Number" in spec["source"]["layers"]


def test_spec_from_figma_frame_falls_back_to_frame_name():
    spec = spec_from_figma_frame({"name": "Numbers That Matter"}, brand=None,
                                 project_id="fig", index=0)
    assert spec["text"]["headline"] == "Numbers That Matter"


def test_spec_from_figma_frame_lints_clean():
    from lib.motion_compiler import lint_spec

    for name in ("Hero Cover", "Two Column Split", "Card Grid", "Quote Plate",
                 "Editorial Article", "Orbit Badge", "Plain Frame"):
        spec = spec_from_figma_frame({"name": name}, brand=None, project_id="fig", index=0)
        assert lint_spec(spec) == [], (name, lint_spec(spec))


def test_spec_from_figma_frame_uses_channel_palette():
    from lib.brand_profile import load_brand

    brand = load_brand("tech-deep")
    spec = spec_from_figma_frame(FRAME, brand=brand, project_id="fig", index=0)
    assert spec["palette"]["bgBase"]
    assert spec["palette"]["accent"]


def test_spec_from_figma_frame_accepts_a_bare_string():
    spec = spec_from_figma_frame("Hero Cover", brand=None, project_id="fig", index=0)
    assert spec["scheme"] == "monolith"


# ---------------------------------------------------------------------------
# Status + stubbed tool calls
# ---------------------------------------------------------------------------

def test_status_is_offline_and_reports_off(monkeypatch):
    """get_status() must never touch the network — registry discovery calls it."""
    from tools.integrations.figma_mcp import figma_status

    for var in ("FIGMA_TOKEN", "FIGMA_MCP_URL", "FIGMA_MCP_REMOTE_URL",
                "FIGMA_MCP_DESKTOP", "FIGMA_FILE_KEY"):
        monkeypatch.delenv(var, raising=False)
    status = figma_status()
    assert status["mode"] == "off"
    assert status["configured"] is False
    assert "mcp_error" not in status


def test_status_reports_mcp_configured_without_connecting(monkeypatch):
    from tools.integrations.figma_mcp import figma_status

    monkeypatch.setenv("FIGMA_MCP_URL", "https://mcp.figma.com/mcp")
    status = figma_status()
    assert status["mode"] == "mcp-configured"
    assert status["configured"] is True


def test_status_reports_rest_when_only_a_token_exists(monkeypatch):
    from tools.integrations.figma_mcp import figma_status

    monkeypatch.delenv("FIGMA_MCP_URL", raising=False)
    monkeypatch.delenv("FIGMA_MCP_DESKTOP", raising=False)
    monkeypatch.delenv("FIGMA_MCP_REMOTE_URL", raising=False)
    monkeypatch.setenv("FIGMA_TOKEN", "fake")
    status = figma_status()
    assert status["mode"] == "rest-tokens"


def test_probe_reports_the_transport_failure(monkeypatch):
    from tools.integrations.figma_mcp import figma_probe

    monkeypatch.delenv("FIGMA_TOKEN", raising=False)
    client = FigmaMCPClient(url="http://127.0.0.1:9/mcp", timeout=1.0)
    status = figma_probe(client)
    assert status["mcp_available"] is False
    assert "mcp_error" in status


def test_tool_calls_are_wrapped_correctly():
    calls: list[tuple[str, dict]] = []

    class Stub(FigmaMCPClient):
        def _rpc(self, method, params=None, *, notify=False):
            if method == "initialize":
                return {}
            if method == "notifications/initialized":
                return None
            if method == "tools/list":
                return {"tools": [{"name": "get_design_context"},
                                  {"name": "get_screenshot"}]}
            if method == "tools/call":
                calls.append((params["name"], params["arguments"]))
                return {"content": [{"type": "text", "text": '{"ok": true}'}]}
            return {}

    stub = Stub(url="http://stub/mcp")
    assert stub.has_tool("get_design_context") is True
    assert stub.has_tool("nope") is False

    assert stub.design_context("1:23", file_key="abc") == {"ok": True}
    assert calls[0] == ("get_design_context",
                        {"nodeId": "1:23", "fileKey": "abc", "clientFrameworks": ["react"]})

    stub.variable_defs("1:23", "abc")
    assert calls[1] == ("get_variable_defs", {"nodeId": "1:23", "fileKey": "abc"})

    stub.screenshot("1:23")
    assert calls[2] == ("get_screenshot", {"nodeId": "1:23"})
