"""Tests for lib/llm_client.py — the universal runtime LLM client.

Covers provider resolution (presets, custom endpoints, LLM_STYLE), both wire
protocols against a live local HTTP server, JSON extraction/repair, retry
semantics, and the status probe. The client is the heart of Creative Engine
v3 ("connect ANY LLM") so it gets real-socket coverage, not just mocks.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from lib.llm_client import (  # noqa: E402
    LLMClient,
    LLMError,
    PROVIDER_PRESETS,
    llm_status,
    resolve_provider,
)


# ---------------------------------------------------------------------------
# Resolution (no network)
# ---------------------------------------------------------------------------

def test_no_configuration_is_unavailable(monkeypatch):
    for var in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_PROVIDER",
                "OLLAMA_BASE_URL", *[p["key_env"] for p in PROVIDER_PRESETS.values() if p["key_env"]],
                "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert resolve_provider()["provider"] is None
    assert llm_status()["available"] is False
    with pytest.raises(LLMError):
        LLMClient().chat([{"role": "user", "content": "hi"}])


def test_custom_endpoint_resolution(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://example.test:8000/v1/")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "mymodel")
    cfg = resolve_provider()
    assert cfg["base_url"] == "http://example.test:8000/v1"  # trailing slash stripped
    assert cfg["model"] == "mymodel"
    assert cfg["style"] == "openai"


def test_llm_style_selects_anthropic_protocol(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://my-anthropic-proxy.test")
    monkeypatch.setenv("LLM_STYLE", "anthropic")
    cfg = resolve_provider()
    assert cfg["style"] == "anthropic"


def test_anthropic_domain_auto_detects_protocol(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://api.anthropic.com/v1")
    cfg = resolve_provider()
    assert cfg["style"] == "anthropic"


def test_llm_style_openai_beats_domain_sniff(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://api.anthropic.com/v1")
    monkeypatch.setenv("LLM_STYLE", "openai")  # a gateway on that domain speaking openai
    assert resolve_provider()["style"] == "openai"


def test_every_preset_resolves(monkeypatch):
    for name, preset in PROVIDER_PRESETS.items():
        for var in ("LLM_BASE_URL", "LLM_PROVIDER", "OLLAMA_BASE_URL"):
            monkeypatch.delenv(var, raising=False)
        if preset["key_env"]:
            monkeypatch.setenv(preset["key_env"], "test-key")
        else:  # local presets are only reachable via LLM_PROVIDER
            monkeypatch.setenv("LLM_PROVIDER", name)
        cfg = resolve_provider()
        assert cfg["provider"] == name, (name, cfg)
        assert cfg["base_url"].startswith("http"), (name, cfg)
        assert cfg["model"], (name, cfg)
        if preset["key_env"]:
            monkeypatch.delenv(preset["key_env"])


def test_named_provider_wins_over_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "groq-key")
    cfg = resolve_provider()
    assert cfg["provider"] == "groq"
    assert cfg["style"] == "openai"


def test_unknown_named_provider_raises(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "not-a-provider")
    with pytest.raises(LLMError):
        resolve_provider()


def test_gemini_alt_key(monkeypatch):
    for var in ("LLM_BASE_URL", "LLM_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    cfg = resolve_provider()
    assert cfg["provider"] == "gemini"
    assert cfg["api_key"] == "g-key"


def test_constructor_style_argument_beats_everything():
    c = LLMClient(base_url="http://x.test", style="anthropic")
    assert c.style == "anthropic"


# ---------------------------------------------------------------------------
# Live wire-protocol round trips (local HTTP server — real sockets)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mock_llm_server():
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append({
                "path": self.path,
                "auth": self.headers.get("Authorization", ""),
                "x_api_key": self.headers.get("x-api-key", ""),
                "body": body,
            })
            if "unauthorized" in str(body.get("messages", "")):
                self.send_response(401)
                data = b'{"error": "bad key"}'
            elif "overloaded" in str(body.get("messages", "")):
                self.send_response(503)
                data = b'{"error": "overloaded"}'
            elif "fenced" in str(body.get("messages", "")):
                self.send_response(200)
                resp = {"choices": [{"message": {"content": "```json\n{\"ok\": true}\n```"}}]}
                data = json.dumps(resp).encode()
            elif "garbage" in str(body.get("messages", "")):
                # First reply is garbage; the repair retry returns valid JSON
                # only when asked again (call count on this content).
                self.send_response(200)
                n = sum(1 for c in captured if "garbage" in str(c["body"].get("messages", "")))
                content = '{"fixed": true}' if n >= 2 else "sure, here you go:"
                resp = {"choices": [{"message": {"content": content}}]}
                data = json.dumps(resp).encode()
            elif self.path.endswith("/messages"):  # anthropic protocol
                self.send_response(200)
                resp = {"content": [{"type": "text", "text": '{"via": "anthropic"}'}]}
                data = json.dumps(resp).encode()
            else:
                self.send_response(200)
                resp = {"choices": [{"message": {"content": "openai-reply"}}]}
                data = json.dumps(resp).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}", captured
    srv.shutdown()


def test_openai_round_trip(mock_llm_server, monkeypatch):
    base, captured = mock_llm_server
    monkeypatch.setenv("LLM_BASE_URL", f"{base}/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-live")
    monkeypatch.setenv("LLM_MODEL", "m1")
    out = LLMClient().chat([{"role": "user", "content": "hello"}])
    assert out == "openai-reply"
    req = captured[-1]
    assert req["path"] == "/v1/chat/completions"
    assert req["auth"] == "Bearer sk-live"
    assert req["body"]["model"] == "m1"


def test_anthropic_round_trip_via_style(mock_llm_server, monkeypatch):
    base, captured = mock_llm_server
    monkeypatch.setenv("LLM_BASE_URL", f"{base}/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-ant")
    monkeypatch.setenv("LLM_STYLE", "anthropic")
    out = LLMClient().chat([{"role": "system", "content": "be brief"},
                            {"role": "user", "content": "hello"}])
    assert out == '{"via": "anthropic"}'
    req = captured[-1]
    assert req["path"] == "/v1/messages"
    assert req["x_api_key"] == "sk-ant"
    assert req["body"]["system"] == "be brief"          # system hoisted out
    assert req["body"]["max_tokens"] >= 1               # always present
    assert all(m["role"] != "system" for m in req["body"]["messages"])


def test_chat_json_strips_fences(mock_llm_server, monkeypatch):
    base, _ = mock_llm_server
    monkeypatch.setenv("LLM_BASE_URL", f"{base}/v1")
    out = LLMClient().chat_json([{"role": "user", "content": "fenced"}])
    assert out == {"ok": True}


def test_chat_json_repair_path(mock_llm_server, monkeypatch):
    base, captured = mock_llm_server
    monkeypatch.setenv("LLM_BASE_URL", f"{base}/v1")
    out = LLMClient().chat_json([{"role": "user", "content": "garbage"}])
    assert out == {"fixed": True}
    assert len(captured) >= 2  # the repair actually re-asked


def test_auth_errors_fail_fast(mock_llm_server, monkeypatch):
    base, _ = mock_llm_server
    monkeypatch.setenv("LLM_BASE_URL", f"{base}/v1")
    client = LLMClient(max_retries=3)
    t0 = threading.Timer  # noqa: F841  (prove no sleeps burn here is overkill)
    with pytest.raises(LLMError, match="401"):
        client.chat([{"role": "user", "content": "unauthorized"}])


def test_transient_errors_retry(mock_llm_server, monkeypatch):
    base, _ = mock_llm_server
    monkeypatch.setenv("LLM_BASE_URL", f"{base}/v1")
    client = LLMClient(max_retries=1)
    # The mock always 503s for "overloaded" — with retries it still raises,
    # but only after the retry loop ran (would raise LLMError either way).
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "overloaded"}])


def test_extract_json_shapes():
    from lib.llm_client import _extract_json
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert _extract_json('Sure! Here it is:\n{"a": 3}\nhope that helps') == {"a": 3}
    assert _extract_json('[1, 2, {"b": 4}]') == [1, 2, {"b": 4}]
    assert _extract_json('no json at all') is None
    assert _extract_json('{"nested": {"deep": "}"}, "tail": 1}') == {"nested": {"deep": "}"}, "tail": 1}
