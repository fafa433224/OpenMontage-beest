"""Tests for `backlot serve --host` / BACKLOT_HOST bind-address support.

`cmd_serve` used to hardcode host="127.0.0.1", which makes the board
unreachable from outside the machine that runs it — a hard blocker in
containers, devcontainers, sandboxes and any setup where the browser is not
on the same host as the server (e.g. a proxied live preview). The default
stays loopback for safety; opting in is explicit via --host or BACKLOT_HOST.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backlot import __main__ as backlot_main  # noqa: E402


@pytest.fixture
def captured_uvicorn(monkeypatch):
    """Intercept uvicorn.run and record its keyword arguments."""
    seen: dict = {}

    class FakeUvicorn:
        @staticmethod
        def run(app, **kwargs):
            seen["app"] = app
            seen.update(kwargs)

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)
    return seen


def test_host_defaults_to_loopback(captured_uvicorn, monkeypatch):
    monkeypatch.delenv("BACKLOT_HOST", raising=False)

    assert backlot_main.cmd_serve(4750) == 0
    assert captured_uvicorn["host"] == "127.0.0.1"
    assert captured_uvicorn["port"] == 4750


def test_host_flag_overrides_default(captured_uvicorn, monkeypatch):
    monkeypatch.delenv("BACKLOT_HOST", raising=False)

    assert backlot_main.cmd_serve(4750, "0.0.0.0") == 0
    assert captured_uvicorn["host"] == "0.0.0.0"


def test_host_env_var_is_honoured(captured_uvicorn, monkeypatch):
    monkeypatch.setenv("BACKLOT_HOST", "0.0.0.0")

    assert backlot_main.cmd_serve(4750) == 0
    assert captured_uvicorn["host"] == "0.0.0.0"


def test_host_flag_wins_over_env(captured_uvicorn, monkeypatch):
    monkeypatch.setenv("BACKLOT_HOST", "0.0.0.0")

    assert backlot_main.cmd_serve(4750, "127.0.0.1") == 0
    assert captured_uvicorn["host"] == "127.0.0.1"


def test_cli_parses_host_flag(captured_uvicorn, monkeypatch):
    monkeypatch.delenv("BACKLOT_HOST", raising=False)

    assert backlot_main.main(["serve", "--port", "4800", "--host", "0.0.0.0"]) == 0
    assert captured_uvicorn["host"] == "0.0.0.0"
    assert captured_uvicorn["port"] == 4800


def test_cli_serve_without_host_uses_env(monkeypatch, captured_uvicorn):
    monkeypatch.setenv("BACKLOT_HOST", "0.0.0.0")

    assert backlot_main.main(["serve", "--port", "4801"]) == 0
    assert captured_uvicorn["host"] == "0.0.0.0"
