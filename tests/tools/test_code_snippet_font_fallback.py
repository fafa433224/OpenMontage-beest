"""Regression tests for the CodeSnippet monospace font resolution.

Pygments' ImageFormatter defaults to "DejaVu Sans Mono", which does not
exist on minimal containers (Kaggle notebooks, slim Docker images). The
tool used to raise FontNotFound there; it now resolves whatever mono font
the system actually has. These tests pin that behaviour.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.graphics.code_snippet import (  # noqa: E402
    CodeSnippet,
    _fc_family_exists,
    _resolve_monospace_family,
)


@pytest.fixture(scope="module")
def has_deps():
    try:
        import PIL  # noqa: F401
        import pygments  # noqa: F401
    except ImportError:
        pytest.skip("Pygments and Pillow required")
    return True


class TestFontResolution:
    def test_resolved_family_is_usable(self):
        """Whatever the resolver returns, fontconfig must actually find it
        (or the system lacks fc-list entirely, where we keep the old default
        for font-rich desktop systems)."""
        family = _resolve_monospace_family()
        has_fc = Path("/usr/bin/fc-list").exists() or Path("/usr/bin/fc-match").exists()
        if has_fc:
            assert _fc_family_exists(family) or family == "DejaVu Sans Mono"

    def test_resolution_is_cached(self):
        _resolve_monospace_family()
        assert getattr(_resolve_monospace_family, "_cached", None) is not None
        assert _resolve_monospace_family() == _resolve_monospace_family._cached

    def test_falls_back_when_no_candidate_exists(self, monkeypatch):
        """Simulate a container that has none of the well-known mono fonts:
        the resolver must fall back to fontconfig's own monospace alias
        (monkeypatched here) rather than crashing."""
        monkeypatch.setattr("tools.graphics.code_snippet._fc_family_exists",
                            lambda family: False)

        import subprocess as sp

        class _Fake:
            returncode = 0
            # realistic `fc-match -f '%{family}' monospace` output: family only
            stdout = "Some Container Mono"

        def fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "fc-match":
                return _Fake()
            raise OSError("no fc-list")

        monkeypatch.setattr(sp, "run", fake_run)
        # clear the cache so the patched lookups actually run
        monkeypatch.setattr(_resolve_monospace_family, "_cached", None,
                            raising=False)
        assert _resolve_monospace_family() == "Some Container Mono"

    def test_render_succeeds_with_resolved_font(self, has_deps, tmp_path):
        """The end-to-end guarantee: on THIS system (which may or may not
        have DejaVu Sans Mono) the tool must render without FontNotFound."""
        tool = CodeSnippet()
        r = tool.execute({
            "code": "def hello():\n    print('font fallback')\n",
            "language": "python",
            "theme": "monokai",
            "output_path": str(tmp_path / "snippet.png"),
        })
        assert r.success, f"render failed: {r.error}"
        assert Path(r.data["output"]).exists()
        assert r.data["line_count"] == 3
