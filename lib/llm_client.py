"""Universal LLM client — connect ANY model provider through one interface.

This is the runtime-LLM layer that was missing from OpenMontage. The repo's
original design was "the IDE agent is the LLM", which works when a human
drives the pipeline but produces nothing creative in headless runs (Kaggle,
CI, cron). This client fixes that: any OpenAI-compatible endpoint — and the
major native APIs — can drive creative decisions at runtime.

Design goals
------------
- **Zero hard dependencies.** Pure stdlib (urllib) so the client imports on
  any Python, even minimal Kaggle/CI containers. `pip install openai` is
  never required.
- **One env contract.** ``LLM_BASE_URL`` + ``LLM_API_KEY`` + ``LLM_MODEL``
  covers every OpenAI-compatible provider at once (OpenAI, DeepSeek, Groq,
  OpenRouter, Together, Fireworks, GitHub Models, LiteLLM, vLLM, TabbyAPI,
  llama.cpp server, Ollama's /v1 bridge, Kaggle Model endpoints, ...).
- **Provider presets.** Setting only ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY``
  / ``GEMINI_API_KEY`` / ``GROQ_API_KEY`` / ``DEEPSEEK_API_KEY`` /
  ``OPENROUTER_API_KEY`` / ``MISTRAL_API_KEY`` / ``TOGETHER_API_KEY`` /
  ``FIREWORKS_API_KEY`` / ``XAI_API_KEY`` (or ``GOOGLE_API_KEY``) is enough;
  base URLs and default models are resolved from a preset table.
- **Local first.** ``Ollama`` and ``llama.cpp`` need no key at all; the
  client auto-detects ``http://localhost:11434/v1`` when OLLAMA_BASE_URL
  or the ollama binary is present.
- **Robust JSON.** ``chat_json`` strips markdown fences, finds the first
  JSON object/array in the reply, and retries once with a repair prompt.

Provider count: 14 presets + any custom OpenAI-compatible endpoint.
That means every hosted model reachable through those 14 gateways works
(GPT, Claude, Gemini, Llama, Qwen, DeepSeek-R1, Mistral, Grok, Kimi, ...)
plus every local model via Ollama/vLLM/llama.cpp.

Usage
-----
    from lib.llm_client import LLMClient, llm_status

    client = LLMClient()                    # auto-resolves from env
    reply = client.chat([{"role": "user", "content": "..."}])
    plan  = client.chat_json([...])         # parsed dict / list
    if llm_status()["available"]: ...

All calls raise ``LLMError`` (never bare urllib exceptions) so callers can
feature-detect and fall back to the deterministic creative director.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Optional

__all__ = [
    "LLMError",
    "LLMClient",
    "llm_status",
    "PROVIDER_PRESETS",
    "resolve_provider",
]

# ---------------------------------------------------------------------------
# Provider presets — a preset is: base_url, key_env, default_model, style
# style: "openai" (chat/completions), "anthropic" (messages), "gemini"
# ---------------------------------------------------------------------------
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
        "style": "openai",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "key_env": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-20250514",
        "style": "anthropic",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY",
        "alt_key_env": "GOOGLE_API_KEY",
        "default_model": "gemini-2.0-flash",
        "style": "openai",  # Google exposes an OpenAI-compatible bridge
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "default_model": "llama-3.3-70b-versatile",
        "style": "openai",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
        "style": "openai",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "default_model": "openai/gpt-4o-mini",
        "style": "openai",
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "key_env": "MISTRAL_API_KEY",
        "default_model": "mistral-large-latest",
        "style": "openai",
    },
    "together": {
        "base_url": "https://api.together.xyz/v1",
        "key_env": "TOGETHER_API_KEY",
        "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "style": "openai",
    },
    "fireworks": {
        "base_url": "https://api.fireworks.ai/inference/v1",
        "key_env": "FIREWORKS_API_KEY",
        "default_model": "accounts/fireworks/models/llama-v3p3-70b-instruct",
        "style": "openai",
    },
    "xai": {
        "base_url": "https://api.x.ai/v1",
        "key_env": "XAI_API_KEY",
        "default_model": "grok-2-latest",
        "style": "openai",
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "key_env": "MOONSHOT_API_KEY",
        "default_model": "moonshot-v1-8k",
        "style": "openai",
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "key_env": "ZHIPU_API_KEY",
        "default_model": "glm-4-flash",
        "style": "openai",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "key_env": "",  # local server, no key
        "default_model": "qwen2.5:7b",
        "style": "openai",
    },
    "vllm": {
        "base_url": "http://localhost:8000/v1",
        "key_env": "",  # local server, no key
        "default_model": "auto",
        "style": "openai",
    },
}


class LLMError(RuntimeError):
    """Any LLM failure — network, auth, quota, malformed reply."""


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def resolve_provider() -> dict[str, Any]:
    """Resolve the active LLM configuration from the environment.

    Priority:
    1. ``LLM_BASE_URL`` (+ optional ``LLM_API_KEY`` / ``LLM_MODEL``) — any
       OpenAI-compatible endpoint wins because it is the most explicit.
    2. ``LLM_PROVIDER`` — named preset ("anthropic", "groq", "ollama", ...).
    3. First preset whose key env var is set (deterministic table order).
    4. Local servers probed cheaply (no network): OLLAMA_BASE_URL env.
    """
    # 1. Explicit custom endpoint
    base = _env("LLM_BASE_URL")
    if base:
        return {
            "provider": _env("LLM_PROVIDER") or "custom",
            "base_url": base.rstrip("/"),
            "api_key": _env("LLM_API_KEY"),
            "model": _env("LLM_MODEL") or "auto",
            "style": "openai",
        }

    # 2. Named provider
    named = _env("LLM_PROVIDER").lower()
    if named:
        preset = PROVIDER_PRESETS.get(named)
        if preset is None:
            raise LLMError(
                f"LLM_PROVIDER={named!r} is unknown. Presets: "
                + ", ".join(sorted(PROVIDER_PRESETS))
            )
        key = _env(preset["key_env"]) if preset["key_env"] else ""
        # anthropic via native style also accepts ANTHROPIC_AUTH_TOKEN
        if not key and named == "anthropic":
            key = _env("ANTHROPIC_AUTH_TOKEN")
        return {
            "provider": named,
            "base_url": preset["base_url"],
            "api_key": key,
            "model": _env("LLM_MODEL") or preset["default_model"],
            "style": preset["style"],
        }

    # 3. Key-based auto-detection (table order = sensible default priority)
    for name, preset in PROVIDER_PRESETS.items():
        if not preset["key_env"]:
            continue
        key = _env(preset["key_env"])
        if not key:
            alt = preset.get("alt_key_env")
            key = _env(alt) if alt else ""
        if key:
            return {
                "provider": name,
                "base_url": preset["base_url"],
                "api_key": key,
                "model": _env("LLM_MODEL") or preset["default_model"],
                "style": preset["style"],
            }

    # 4. Local Ollama bridge if explicitly pointed at
    ollama_base = _env("OLLAMA_BASE_URL")
    if ollama_base:
        return {
            "provider": "ollama",
            "base_url": ollama_base.rstrip("/").removesuffix("/") + "/v1",
            "api_key": "",
            "model": _env("LLM_MODEL") or PROVIDER_PRESETS["ollama"]["default_model"],
            "style": "openai",
        }

    return {"provider": None, "base_url": "", "api_key": "", "model": "", "style": "openai"}


class LLMClient:
    """One chat client for every provider. Construct with no arguments to
    resolve everything from the environment, or pass explicit values for
    tests / multi-model fan-out."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        timeout: float = 120.0,
        max_retries: int = 2,
    ) -> None:
        resolved = resolve_provider()
        self.base_url = (base_url or resolved["base_url"]).rstrip("/")
        self.api_key = api_key if api_key is not None else resolved["api_key"]
        self.model = model or resolved["model"]
        self.provider = provider or resolved["provider"] or "custom"
        self.style = resolved["style"] if base_url is None else "openai"
        self.timeout = timeout
        self.max_retries = max_retries
        self.available = bool(self.base_url)

    # ------------------------------------------------------------------
    # Core chat
    # ------------------------------------------------------------------
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        system: Optional[str] = None,
    ) -> str:
        """Send a chat, return the assistant text.

        ``system`` is a convenience — it is prepended to ``messages`` so
        callers don't rebuild the list for one-prompt calls.
        """
        if not self.available:
            raise LLMError(
                "No LLM configured. Set LLM_BASE_URL (+ LLM_API_KEY, LLM_MODEL), "
                "or a provider key (OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, "
                "GROQ_API_KEY, DEEPSEEK_API_KEY, OPENROUTER_API_KEY, ...), or point "
                "LLM_PROVIDER=ollama at a local server."
            )
        if system:
            messages = [{"role": "system", "content": system}, *messages]

        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._chat_once(messages, temperature, max_tokens)
            except LLMError as exc:
                last_err = exc
                # Retry only transient classes; auth errors fail fast.
                msg = str(exc).lower()
                transient = any(k in msg for k in ("429", "timeout", "502", "503", "504", "connection"))
                if not transient or attempt == self.max_retries:
                    raise
                time.sleep(1.5 * (attempt + 1))
        raise last_err or LLMError("unreachable")

    def _chat_once(
        self,
        messages: list[dict[str, str]],
        temperature: Optional[float],
        max_tokens: Optional[int],
    ) -> str:
        if self.style == "anthropic" and "anthropic.com" in self.base_url:
            return self._chat_anthropic(messages, temperature, max_tokens)
        return self._chat_openai(messages, temperature, max_tokens)

    def _post_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise LLMError(f"HTTP {exc.code} from {url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"connection to {url} failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise LLMError(f"non-JSON reply from {url}: {exc}") from exc

    def _chat_openai(
        self,
        messages: list[dict[str, str]],
        temperature: Optional[float],
        max_tokens: Optional[int],
    ) -> str:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = self._post_json(f"{self.base_url}/chat/completions", payload, headers)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected reply shape: {json.dumps(data)[:400]}") from exc
        if not isinstance(text, str):
            # Some gateways return content-part lists.
            try:
                text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
            except Exception as exc:
                raise LLMError(f"unreadable content: {text!r}") from exc
        return text

    def _chat_anthropic(
        self,
        messages: list[dict[str, str]],
        temperature: Optional[float],
        max_tokens: Optional[int],
    ) -> str:
        # Anthropic wants the system message OUT of the messages array.
        system_text = "\n\n".join(
            m["content"] for m in messages if m.get("role") == "system"
        )
        chat_messages = [m for m in messages if m.get("role") != "system"]
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or 4096,
            "messages": chat_messages,
        }
        if system_text:
            payload["system"] = system_text
        if temperature is not None:
            payload["temperature"] = temperature
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        data = self._post_json(f"{self.base_url}/messages", payload, headers)
        try:
            return "".join(b.get("text", "") for b in data["content"])
        except (KeyError, TypeError) as exc:
            raise LLMError(f"unexpected anthropic reply: {json.dumps(data)[:400]}") from exc

    # ------------------------------------------------------------------
    # Structured output
    # ------------------------------------------------------------------
    def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: Optional[float] = 0.8,
        max_tokens: Optional[int] = None,
        system: Optional[str] = None,
    ) -> Any:
        """Chat expecting a JSON reply; returns the parsed object.

        Robust to markdown fences and preamble chatter. On a total parse
        failure it retries once with a repair instruction before raising.
        """
        reply = self.chat(messages, temperature=temperature, max_tokens=max_tokens, system=system)
        parsed = _extract_json(reply)
        if parsed is not None:
            return parsed
        repair = [
            *messages,
            {"role": "assistant", "content": reply},
            {
                "role": "user",
                "content": "Your previous reply was not parseable JSON. Reply AGAIN with "
                "ONLY the raw JSON value — no prose, no markdown fences.",
            },
        ]
        reply2 = self.chat(repair, temperature=0.2, max_tokens=max_tokens)
        parsed = _extract_json(reply2)
        if parsed is None:
            raise LLMError(f"LLM did not return JSON. Last reply: {reply2[:400]}")
        return parsed


def _extract_json(text: str) -> Any:
    """Best-effort JSON extraction from an LLM reply."""
    if not text:
        return None
    stripped = text.strip()
    # Strip ```json ... ``` fences
    fenced = re.search(r"```(?:json)?\s*(.+?)```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # First balanced object or array in the text
    for opener, closer in (("{", "}"), ("[", "]")):
        start = stripped.find(opener)
        if start == -1:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(stripped)):
            ch = stripped[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(stripped[start : i + 1])
                    except json.JSONDecodeError:
                        break
        # try next opener
    return None


def llm_status() -> dict[str, Any]:
    """Cheap status probe for preflights and notebooks. Never raises."""
    try:
        cfg = resolve_provider()
    except LLMError as exc:
        return {"available": False, "error": str(exc)}
    if not cfg["base_url"]:
        return {
            "available": False,
            "provider": None,
            "hint": "set LLM_BASE_URL or a provider API key (OPENAI/ANTHROPIC/GEMINI/GROQ/...)",
        }
    return {
        "available": True,
        "provider": cfg["provider"],
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "has_key": bool(cfg["api_key"]),
    }
