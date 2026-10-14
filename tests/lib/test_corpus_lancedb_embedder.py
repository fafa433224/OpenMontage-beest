"""Regression tests for the OpenSoraStore embedder adapter.

History: lib/corpus_lancedb.py once imported a nonexistent ``ClipEmbedder``
class — every ingest row errored with ImportError and the Open-Sora index
silently built ZERO rows on a real Kaggle run (errors were collected but
the build still exited 0). These tests pin the adapter contract to the
functions lib/clip_embedder actually exports, WITHOUT needing lancedb or
torch installed (the lazy property only touches lib.clip_embedder).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _store_without_lancedb():
    """An OpenSoraStore instance whose __init__ (needs lancedb) is skipped."""
    from lib.corpus_lancedb import OpenSoraStore

    store = object.__new__(OpenSoraStore)
    store._embedder = None
    return store


def test_embedder_adapter_exposes_module_functions(monkeypatch):
    """The lazy embedder property must adapt lib.clip_embedder's real API."""
    fake = types.ModuleType("lib.clip_embedder")

    def embed_texts(texts):
        return np.zeros((len(texts), 512), dtype=np.float32)

    def embed_images(paths):
        return np.zeros((len(paths), 512), dtype=np.float32)

    fake.embed_texts = embed_texts
    fake.embed_images = embed_images
    # NB: ``import lib.clip_embedder as _ce`` resolves through the PARENT
    # package attribute (lib.clip_embedder), not just sys.modules — patch
    # BOTH or the real module (and its HF model download) sneaks in.
    monkeypatch.setitem(sys.modules, "lib.clip_embedder", fake)
    import lib  # noqa: E401  (the parent package, already imported by conftest)

    monkeypatch.setattr(lib, "clip_embedder", fake, raising=False)

    store = _store_without_lancedb()
    e = store.embedder
    assert callable(e.embed_texts) and callable(e.embed_images)
    assert e.embed_texts(["a", "b"]).shape == (2, 512)
    assert e.embed_images(["x.jpg"]).shape == (1, 512)


@pytest.mark.live_api
def test_embedder_adapter_against_real_clip_embedder_module():
    """Wire the adapter to the REAL module and call both entry points.

    Runs wherever torch/transformers are importable; skipped otherwise
    (CI boxes without the GPU requirements). This is the test that would
    have caught the Kaggle zero-row index failure."""
    embedder_mod = pytest.importorskip("lib.clip_embedder", reason="module present")
    pytest.importorskip("numpy")
    # clip_embedder lazily loads torch/transformers inside its functions —
    # verify those heavy imports exist before invoking.
    import importlib.util

    for heavy in ("torch", "transformers"):
        if importlib.util.find_spec(heavy) is None:
            pytest.skip(f"{heavy} not installed — GPU requirements missing")

    store = _store_without_lancedb()
    e = store.embedder  # must not raise ImportError
    assert e is store.embedder, "embedder should be cached (singleton)"
    vecs = e.embed_texts(["hello world"])
    assert vecs.shape == (1, 512)
    assert vecs.dtype == np.float32
    assert np.allclose(np.linalg.norm(vecs, axis=1), 1.0, atol=1e-3), \
        "vectors must be L2-normalised (cosine = dot product contract)"


def test_ingest_stats_surface_errors():
    """The stats dict must carry error_count so callers can assert >0 rows.

    The Kaggle failure mode was: 64 errors, 0 rows, exit code 0. Index
    builders must check total_rows, and the stats contract makes that
    possible — pin its keys here."""
    from lib.corpus_lancedb import OpenSoraStore

    required = {"rows_in", "added", "error_count", "total_rows", "errors"}
    src = (REPO / "lib" / "corpus_lancedb.py").read_text()
    # ingest_manifest builds its stats dict inline; assert the keys exist
    # in source (cheap static check that survives without lancedb).
    for key in required:
        assert f'"{key}"' in src, f"stats key {key} missing from ingest_manifest"
