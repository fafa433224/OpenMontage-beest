"""LLM tool — universal chat + creative direction through any provider.

Actions
-------
``chat``
    Plain prompt → text reply. Temperature and max_tokens controllable.
``chat_json``
    Prompt → parsed JSON (robust to fences/preamble, one repair retry).
``creative_brief``
    Script → creative brief (mood, palette family, caption style, pacing,
    b-roll mood words) that ``CreativeDirector`` honours.
``status``
    Report which provider/model the environment resolved.

The connection contract lives in ``lib/llm_client.py``: 14 provider
presets (OpenAI, Anthropic, Gemini, Groq, DeepSeek, OpenRouter, Mistral,
Together, Fireworks, xAI, Moonshot, Zhipu, Ollama, vLLM) plus ANY custom
OpenAI-compatible endpoint via ``LLM_BASE_URL`` / ``LLM_API_KEY`` /
``LLM_MODEL``. That means every hosted model reachable through those
gateways works, plus every local model via Ollama/vLLM/llama.cpp.

Determinism note: LLM replies are inherently non-deterministic. This tool
is marked NON_DETERMINISTIC; callers that need stable output should seed
``CreativeDirector`` and use it as the fallback path (see
``scripts/kaggle/build_explainer_props.py`` for the pattern: LLM shifts
choices, seeded director guarantees a valid plan either way).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolTier,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.llm_client import LLMClient, LLMError, llm_status  # noqa: E402


class LLMGenerate(BaseTool):
    name = "llm_generate"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "llm"
    provider = "openai-compatible"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.NON_DETERMINISTIC if hasattr(Determinism, "NON_DETERMINISTIC") else Determinism.STOCHASTIC
    runtime = ToolRuntime.HYBRID  # REMOTE for hosted APIs, LOCAL for Ollama/vLLM

    dependencies = []  # stdlib-only client — no pip installs required
    install_instructions = (
        "No dependencies. Configure any provider:\n"
        "  Universal: LLM_BASE_URL + LLM_API_KEY + LLM_MODEL (any OpenAI-compatible endpoint)\n"
        "  OpenAI: OPENAI_API_KEY | Anthropic: ANTHROPIC_API_KEY | Gemini: GEMINI_API_KEY\n"
        "  Groq: GROQ_API_KEY | DeepSeek: DEEPSEEK_API_KEY | OpenRouter: OPENROUTER_API_KEY\n"
        "  Mistral/Together/Fireworks/xAI/Moonshot/Zhipu: <PROVIDER>_API_KEY\n"
        "  Local: LLM_PROVIDER=ollama (http://localhost:11434/v1) or vLLM (http://localhost:8000/v1)"
    )
    agent_skills = ["visual-style"]

    capabilities = ["chat", "chat_json", "creative_brief", "status"]

    supports = {
        "any_provider": True,
        "openai_compatible": True,
        "local_models": True,
        "json_mode": True,
    }
    best_for = [
        "headless script writing and scene planning (Kaggle, CI)",
        "creative direction briefs for the seeded creative director",
        "copywriting: titles, kickers, captions, callouts",
        "any task where the IDE agent is unavailable",
    ]
    not_good_for = [
        "deterministic outputs (replies vary run to run)",
        "media generation (use the image/video/audio tools)",
    ]
    fallback_tools = ["creative_director seeded mode"]

    input_schema = {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["chat", "chat_json", "creative_brief", "status"],
                "description": "What to run.",
            },
            "prompt": {
                "type": "string",
                "description": "User prompt for chat / chat_json.",
            },
            "system": {
                "type": "string",
                "description": "System prompt (persona / contract).",
            },
            "script": {
                "type": "object",
                "description": "Script dict for creative_brief (sections with heading/text).",
            },
            "temperature": {"type": "number", "default": 0.8},
            "max_tokens": {"type": "integer", "default": 2048},
        },
    }

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        action = inputs.get("action", "chat")
        started = time.time()

        if action == "status":
            status = llm_status()
            return ToolResult(
                success=True,
                data={
                    "status": status,
                    "providers_supported": sorted(
                        ["openai", "anthropic", "gemini", "groq", "deepseek",
                         "openrouter", "mistral", "together", "fireworks",
                         "xai", "moonshot", "zhipu", "ollama", "vllm",
                         "custom (LLM_BASE_URL)"]
                    ),
                    "action": action,
                },
            )

        try:
            client = LLMClient()
        except LLMError as exc:
            return ToolResult(
                success=False,
                error=f"LLM not configured: {exc}",
                metadata={"action": action},
            )

        try:
            if action == "chat":
                if not inputs.get("prompt"):
                    return ToolResult(success=False, error="prompt is required for chat",
                                      data={"action": action})
                reply = client.chat(
                    [{"role": "user", "content": inputs["prompt"]}],
                    system=inputs.get("system"),
                    temperature=inputs.get("temperature", 0.8),
                    max_tokens=inputs.get("max_tokens", 2048),
                )
                data: dict[str, Any] = {"reply": reply, "model": client.model,
                                        "provider": client.provider}
            elif action == "chat_json":
                if not inputs.get("prompt"):
                    return ToolResult(success=False, error="prompt is required for chat_json",
                                      data={"action": action})
                parsed = client.chat_json(
                    [{"role": "user", "content": inputs["prompt"]}],
                    system=inputs.get("system"),
                    temperature=inputs.get("temperature", 0.8),
                    max_tokens=inputs.get("max_tokens", 2048),
                )
                data = {"json": parsed, "model": client.model, "provider": client.provider}
            elif action == "creative_brief":
                script = inputs.get("script")
                if not isinstance(script, dict) or not script.get("sections"):
                    return ToolResult(success=False,
                                      error="script with sections[] is required for creative_brief",
                                      data={"action": action})
                from lib.creative_director import llm_creative_brief
                brief = llm_creative_brief(script, client)
                if brief is None:
                    return ToolResult(
                        success=False,
                        error="LLM did not return a usable brief; use the seeded creative director",
                        data={"action": action},
                    )
                data = {"brief": brief, "model": client.model, "provider": client.provider}
            else:
                return ToolResult(success=False, error=f"unknown action {action!r}",
                                  data={"action": action})
        except LLMError as exc:
            return ToolResult(success=False, error=str(exc),
                              data={"action": action, "provider": client.provider})

        return ToolResult(
            success=True,
            data={**data, "action": action},
            duration_seconds=time.time() - started,
            model=client.model,
        )
