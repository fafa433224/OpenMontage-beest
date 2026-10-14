"""Exit-code contract for ``tools/video/open_sora_index.py build``.

History: a real Kaggle run once built the Open-Sora LanceDB index with
``rows_in=64, total_rows=0, error_count=64`` and STILL exited 0 — the
notebook's ``assert returncode == 0`` passed, the renderer silently fell
back to synth b-roll, and the run looked green. These tests pin the fix:
a non-empty manifest that yields a zero-row index must exit non-zero.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.video import open_sora_index as osi  # noqa: E402


def _run_main(monkeypatch, stats):
    """Invoke the CLI build branch with build_index patched to ``stats``."""
    calls = {}

    def fake_build_index(root, *, limit=None, batch=64, db_dir=None):
        calls["root"] = root
        return stats

    monkeypatch.setattr(osi, "build_index", fake_build_index)
    return osi.main(["build", "--dataset-dir", "/tmp/whatever"], ), calls


def test_zero_row_index_exits_nonzero(monkeypatch, capsys):
    stats = {
        "rows_in": 64,
        "added": 0,
        "error_count": 64,
        "total_rows": 0,
        "errors": ["cannot import name 'ClipEmbedder' ..."],
    }
    rc, _ = _run_main(monkeypatch, stats)
    assert rc == 1, "zero-row index from non-empty manifest must fail loudly"
    err = capsys.readouterr().err
    assert "0" in err and "64" in err  # surfaces the row/error counts


def test_partial_errors_still_exit_zero_with_warning(monkeypatch, capsys):
    stats = {
        "rows_in": 64,
        "added": 60,
        "error_count": 4,
        "total_rows": 60,
        "errors": ["clip 12: corrupt file"],
    }
    rc, _ = _run_main(monkeypatch, stats)
    assert rc == 0, "partial failure with a usable index is a warning, not an error"
    assert "WARNING" in capsys.readouterr().err


def test_clean_build_exits_zero(monkeypatch, capsys):
    stats = {
        "rows_in": 64,
        "added": 64,
        "error_count": 0,
        "total_rows": 64,
        "errors": [],
    }
    rc, _ = _run_main(monkeypatch, stats)
    assert rc == 0
    assert capsys.readouterr().err == ""


def test_empty_manifest_build_exits_zero(monkeypatch):
    """No rows in -> nothing to embed -> not an embedding failure."""
    stats = {"rows_in": 0, "added": 0, "error_count": 0, "total_rows": 0, "errors": []}
    rc, _ = _run_main(monkeypatch, stats)
    assert rc == 0
