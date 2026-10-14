"""Open-Sora-Dataset source adapter — local PKU-YuanGroup corpus retrieval.

Replaces web scraping for stock footage. The PKU-YuanGroup/Open-Sora-Dataset
ships ~40k videos (about 1 TB) with CSV metadata (video id, path, caption).
On Kaggle the dataset is downloaded to the 1 TB /kaggle/temp volume, indexed
once with CLIP into LanceDB (see ``tools/video/open_sora_index.py``), and
then this adapter serves semantic search over it — no API keys, no scraping,
no rate limits, full offline reuse.

Search flow
-----------
1. ``OPEN_SORA_LANCEDB`` (or ``dataset_dir/lancedb``) holds the index.
2. ``search(query)`` → ``OpenSoraStore.rank_by_text`` → Candidates pointing
   at LOCAL files.
3. ``download`` copies the local file into the corpus clips dir (cheap —
   same disk) so downstream tooling sees the normal corpus layout.

Env
---
- ``OPEN_SORA_DIR``: dataset root (contains videos + CSV manifest).
- ``OPEN_SORA_LANCEDB``: LanceDB dir (default ``<OPEN_SORA_DIR>/lancedb``).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .base import Candidate, SearchFilters

try:  # the repo root is importable in tool runs; guard for isolated tests
    from lib.corpus_lancedb import OpenSoraStore, lancedb_available
except Exception:  # pragma: no cover
    lancedb_available = lambda: False  # noqa: E731
    OpenSoraStore = None


def _dataset_dir() -> Path:
    env = os.environ.get("OPEN_SORA_DIR", "").strip()
    if env:
        return Path(env)
    return Path("/kaggle/temp/open-sora-dataset")


class OpenSoraDatasetSource:
    """Local Open-Sora-Dataset retrieval (LanceDB + CLIP)."""

    name = "open_sora_dataset"
    display_name = "Open-Sora-Dataset (local LanceDB+CLIP)"
    priority = 5  # above every scraper — local, free, fast
    supports = {"video": True, "image": False, "local": True, "semantic_search": True}
    install_instructions = (
        "1. Download the dataset (Kaggle: /kaggle/temp has 1 TB):\n"
        "   huggingface-cli download pku-yuan/open-sora-dataset --repo-type dataset --local-dir $OPEN_SORA_DIR\n"
        "2. pip install lancedb\n"
        "3. Build the index: python tools/video/open_sora_index.py build --dataset-dir $OPEN_SORA_DIR\n"
        "Set OPEN_SORA_DIR to the dataset root."
    )

    def __init__(self) -> None:
        self._store: Any = None

    def is_available(self) -> bool:
        if not lancedb_available():
            return False
        return Path(self._db_path()).exists()

    def _db_path(self) -> str:
        import os

        return (
            os.environ.get("OPEN_SORA_LANCEDB", "").strip()
            or str(_dataset_dir() / "lancedb")
        )

    def _get_store(self) -> Any:
        if self._store is None:
            db = os.environ.get("OPEN_SORA_LANCEDB", "").strip() or str(_dataset_dir() / "lancedb")
            self._store = OpenSoraStore(db)
        return self._store

    def search(self, query: str, filters: SearchFilters) -> list[Candidate]:
        # Contract: an unavailable local index means "no results", not an
        # exception — the corpus builder skips us via is_available(), and
        # direct get_source() callers get a legal empty list.
        if not lancedb_available():
            return []
        db = self._db_path()
        if not Path(db).exists():
            return []
        try:
            store = self._get_store()
            hits = store.rank_by_text(query, k=max(filters.per_page, 20))
        except Exception:
            # A corrupt/unreadable index must not poison a fan-out run;
            # surface as "no results" and let preflights report the cause.
            return []
        out: list[Candidate] = []
        for h in hits:
            path = h.get("local_path", "")
            if not path or not Path(path).is_file():
                p = _dataset_dir() / path if not Path(path).is_absolute() else Path(path)
                if not p.is_file():
                    continue
                path = str(p)
            duration = h.get("duration") or 0.0
            if filters.min_duration and duration and duration < filters.min_duration:
                continue
            if filters.max_duration and duration and duration > filters.max_duration:
                continue
            if filters.orientation == "landscape" and h.get("width", 0) and h.get("height", 0):
                if h["width"] < h["height"]:
                    continue
            if filters.orientation == "portrait" and h.get("width", 0) and h.get("height", 0):
                if h["width"] > h["height"]:
                    continue
            if filters.min_width and h.get("width", 0) and h["width"] < filters.min_width:
                continue
            out.append(
                Candidate(
                    source=self.name,
                    source_id=h.get("clip_id", ""),
                    source_url=f"local://{path}",
                    download_url=path,  # local file — "download" is a copy
                    kind="video",
                    width=int(h.get("width") or 0),
                    height=int(h.get("height") or 0),
                    duration=float(duration),
                    creator="PKU-YuanGroup Open-Sora-Dataset",
                    license="CC BY-NC 4.0 / Open-Sora-Dataset terms",
                    source_tags=h.get("caption", ""),
                )
            )
            if len(out) >= filters.per_page:
                break
        return out

    def download(self, candidate: Candidate, out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        src = Path(candidate.download_url)
        if not src.is_file():
            raise FileNotFoundError(f"Open-Sora clip not found on disk: {src}")
        if src.resolve() == out_path.resolve():
            return out_path
        shutil.copy2(src, out_path)
        return out_path
