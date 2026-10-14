"""Headless web search for research stages (stdlib-only).

Backends, in priority order (first available wins):

1. ``TAVILY_API_KEY``  — Tavily search API (best snippets, generous free tier)
2. ``BRAVE_API_KEY``   — Brave search API
3. ``SERPER_API_KEY``  — Serper.dev (Google results)
4. DuckDuckGo HTML     — no key required, works everywhere (Kaggle included)

Every backend is called with ``urllib`` from the standard library so the
tool stays dependency-free like the rest of the core runtime. Results are
normalized to ``[{title, url, snippet, host, backend}]``.

The tool exists so headless runs (Kaggle harness, CI, cron) can execute
the ``research`` stage of any pipeline without an IDE agent. It is also
referenced by ``pipeline_defs/cinematic.yaml`` and the harness research
stage.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any

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

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_RESULT_LINK_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.S,
)
_RESULT_SNIPPET_RE = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
    re.S,
)


def _http_get(url: str, headers: dict[str, str], timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_post_json(
    url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int = 20
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"User-Agent": _UA, "Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def _decode_ddg_href(href: str) -> str:
    """DuckDuckGo wraps outbound links in /l/?uddg=<urlencoded> redirects."""
    if "uddg=" in href:
        try:
            query = href.split("?", 1)[1] if "?" in href else ""
            params = urllib.parse.parse_qs(query)
            if "uddg" in params and params["uddg"]:
                return params["uddg"][0]
        except Exception:
            pass
    if href.startswith("//"):
        href = "https:" + href
    return href


def _search_tavily(query: str, num: int, recency_days: int | None) -> list[dict[str, Any]]:
    key = os.environ["TAVILY_API_KEY"]
    payload: dict[str, Any] = {
        "api_key": key,
        "query": query,
        "max_results": num,
        "include_answer": False,
    }
    if recency_days:
        payload["search_depth"] = "advanced"
    data = _http_post_json("https://api.tavily.com/search", payload, {})
    out = []
    for r in data.get("results", [])[:num]:
        url = r.get("url") or ""
        out.append(
            {
                "title": (r.get("title") or "").strip(),
                "url": url,
                "snippet": (r.get("content") or "").strip()[:500],
                "host": urllib.parse.urlparse(url).netloc,
                "backend": "tavily",
            }
        )
    return out


def _search_brave(query: str, num: int, recency_days: int | None) -> list[dict[str, Any]]:
    key = os.environ["BRAVE_API_KEY"]
    params = {"q": query, "count": min(num, 20)}
    if recency_days:
        params["freshness"] = f"pd{recency_days}" if recency_days <= 365 else "py"
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(params)
    data = json.loads(_http_get(url, {"X-Subscription-Token": key, "Accept": "application/json"}))
    out = []
    for r in (data.get("web") or {}).get("results", [])[:num]:
        url = r.get("url") or ""
        out.append(
            {
                "title": (r.get("title") or "").strip(),
                "url": url,
                "snippet": _strip_tags(r.get("description") or "")[:500],
                "host": urllib.parse.urlparse(url).netloc,
                "backend": "brave",
            }
        )
    return out


def _search_serper(query: str, num: int, recency_days: int | None) -> list[dict[str, Any]]:
    key = os.environ["SERPER_API_KEY"]
    payload: dict[str, Any] = {"q": query, "num": min(num, 10)}
    data = _http_post_json(
        "https://google.serper.dev/search",
        payload,
        {"X-API-KEY": key},
    )
    out = []
    for r in data.get("organic", [])[:num]:
        url = r.get("link") or ""
        out.append(
            {
                "title": (r.get("title") or "").strip(),
                "url": url,
                "snippet": (r.get("snippet") or "").strip()[:500],
                "host": urllib.parse.urlparse(url).netloc,
                "backend": "serper",
            }
        )
    return out


def _search_duckduckgo(query: str, num: int, recency_days: int | None) -> list[dict[str, Any]]:
    params = {"q": query}
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode(params)
    page = _http_get(url, {}).decode("utf-8", errors="replace")
    links = _RESULT_LINK_RE.findall(page)
    snippets = _RESULT_SNIPPET_RE.findall(page)
    out = []
    seen: set[str] = set()
    for (href, title), idx in zip(links, range(len(links))):
        real_url = _decode_ddg_href(html.unescape(href))
        if not real_url.startswith("http") or real_url in seen:
            continue
        seen.add(real_url)
        snippet = _strip_tags(snippets[idx]) if idx < len(snippets) else ""
        out.append(
            {
                "title": _strip_tags(title)[:200],
                "url": real_url,
                "snippet": snippet[:500],
                "host": urllib.parse.urlparse(real_url).netloc,
                "backend": "duckduckgo",
            }
        )
        if len(out) >= num:
            break
    return out


_BACKENDS = [
    ("tavily", _search_tavily),
    ("brave", _search_brave),
    ("serper", _search_serper),
    ("duckduckgo", _search_duckduckgo),
]


def _active_backend() -> tuple[str, Any]:
    """First key-backed provider wins; DuckDuckGo is the no-key floor."""
    for name, fn in _BACKENDS:
        if name == "duckduckgo":
            continue
        if os.environ.get(name.upper() + "_API_KEY"):
            return name, fn
    return "duckduckgo", _search_duckduckgo


class WebSearch(BaseTool):
    name = "web_search"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "research"
    provider = "duckduckgo"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["network:https"]
    install_instructions = (
        "No install needed. Optional better backends: set TAVILY_API_KEY, "
        "BRAVE_API_KEY, or SERPER_API_KEY in .env; otherwise DuckDuckGo is "
        "used with no key."
    )

    capabilities = ["web_search", "research_queries", "news_search"]
    best_for = [
        "headless research stage grounding (Kaggle harness, CI, cron)",
        "gathering data points, audience questions, and references before scripting",
        "checking current facts for a video topic",
    ]

    input_schema = {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query (2-8 words works best)",
            },
            "num": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "Number of results to return (default 6)",
            },
            "recency_days": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional freshness filter in days",
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=32, vram_mb=0, disk_mb=0, network_required=True
    )
    retry_policy = RetryPolicy(
        max_retries=1, retryable_errors=["urlopen timeout", "HTTP 5", "HTTP 429"]
    )
    idempotency_key_fields = ["query", "num", "recency_days"]
    side_effects = []

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE

    def _resolve_backend(self) -> tuple[str, Any]:
        # Explicit env override wins: WEB_SEARCH_BACKEND=tavily|brave|serper|duckduckgo
        forced = os.environ.get("WEB_SEARCH_BACKEND", "").strip().lower()
        if forced:
            for env_var, fn in _BACKENDS:
                if env_var == forced:
                    key_needed = env_var.upper() + "_API_KEY"
                    if env_var == "duckduckgo" or os.environ.get(key_needed):
                        return env_var, fn
        return _active_backend()

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0  # DuckDuckGo free; key backends are fractions of a cent

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        query = (inputs.get("query") or "").strip()
        if not query:
            return ToolResult(success=False, error="query is required")
        num = min(int(inputs.get("num") or 6), 20)
        recency_days = inputs.get("recency_days")

        start = time.time()
        backend_name, backend_fn = self._resolve_backend()

        errors: list[str] = []
        attempts = [(backend_name, backend_fn)]
        if backend_name != "duckduckgo":
            attempts.append(("duckduckgo", _search_duckduckgo))
        for name, fn in attempts:
            try:
                results = fn(query, num, recency_days)
                if results:
                    return ToolResult(
                        success=True,
                        data={
                            "query": query,
                            "backend": name,
                            "count": len(results),
                            "results": results,
                        },
                        duration_seconds=round(time.time() - start, 2),
                    )
                errors.append(f"{name}: empty results")
            except Exception as exc:  # noqa: BLE001 - normalize backend errors
                errors.append(f"{name}: {exc}")

        return ToolResult(
            success=False,
            error="all web_search backends failed: " + "; ".join(errors[:3]),
            duration_seconds=round(time.time() - start, 2),
        )
