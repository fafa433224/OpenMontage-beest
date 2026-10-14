"""Tests for tools/analysis/web_search.py — the headless research tool.

Hermetic: backend selection + result normalization are tested without
network. One opt-in live test (marked) exercises DuckDuckGo for real.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.analysis import web_search as ws  # noqa: E402
from tools.analysis.web_search import WebSearch  # noqa: E402


@pytest.fixture()
def clean_env(monkeypatch):
    for var in ("TAVILY_API_KEY", "BRAVE_API_KEY", "SERPER_API_KEY",
                "WEB_SEARCH_BACKEND"):
        monkeypatch.delenv(var, raising=False)


def test_registered_in_registry():
    from tools.tool_registry import registry

    registry.discover()
    assert registry.get("web_search") is not None


def test_backend_defaults_to_duckduckgo(clean_env):
    tool = WebSearch()
    name, fn = tool._resolve_backend()
    assert name == "duckduckgo"
    assert fn is ws._search_duckduckgo


def test_backend_prefers_keyed_provider(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    tool = WebSearch()
    name, fn = tool._resolve_backend()
    assert name == "tavily"
    assert fn is ws._search_tavily


def test_forced_backend_env(monkeypatch, clean_env):
    monkeypatch.setenv("WEB_SEARCH_BACKEND", "brave")
    monkeypatch.setenv("BRAVE_API_KEY", "b")
    tool = WebSearch()
    name, _ = tool._resolve_backend()
    assert name == "brave"


def test_forced_backend_without_key_falls_back(monkeypatch, clean_env):
    """Forcing a keyed backend without the key must NOT fake availability."""
    monkeypatch.setenv("WEB_SEARCH_BACKEND", "brave")
    tool = WebSearch()
    name, _ = tool._resolve_backend()
    assert name == "duckduckgo"


def test_ddg_href_decode():
    assert ws._decode_ddg_href(
        "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&rut=abc"
    ) == "https://example.com/a"
    assert ws._decode_ddg_href("https://plain.example/x") == "https://plain.example/x"


def test_empty_query_rejected(clean_env):
    tool = WebSearch()
    result = tool.execute({"query": "   "})
    assert not result.success


def test_input_schema_requires_query():
    tool = WebSearch()
    assert "query" in tool.input_schema["properties"]
    assert tool.input_schema["required"] == ["query"]


@pytest.mark.live
def test_live_duckduckgo_search(clean_env):
    """Opt-in live probe: set OPENMONTAGE_ALLOW_NETWORK=1 (network + rate
    limits permitting). Sandboxed CI blocks outbound sockets by default."""
    import os

    if os.environ.get("OPENMONTAGE_ALLOW_NETWORK") != "1":
        pytest.skip("live network test — set OPENMONTAGE_ALLOW_NETWORK=1")
    tool = WebSearch()
    result = tool.execute({"query": "openmontage video pipeline", "num": 3})
    assert result.success
    assert result.data["backend"] == "duckduckgo"
    assert 1 <= result.data["count"] <= 3
    for hit in result.data["results"]:
        assert hit["url"].startswith("http")
        assert hit["host"]
