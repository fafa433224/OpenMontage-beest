"""LanceDB-backed vector store for the Open-Sora-Dataset corpus.

Why
---
The original corpus is JSONL + .npy with dense cosine math over the full
matrix — fine for hundreds of clips, painful for the Open-Sora-Dataset
(~40k videos, 1 TB). LanceDB gives ANN search, disk-backed storage, and
scalar filtering at that scale while staying a single dependency that
works on Kaggle's T4 instances.

Design
------
- **Drop-in retrieval surface**: exposes ``rank_by_text`` / ``knn`` /
  ``find_similar`` with the same result shapes as ``lib/corpus.py`` so
  callers don't branch on the backend.
- **Same embedding model**: `lib/clip_embedder.py` (CLIP ViT-B/32, 512-d,
  L2-normalised). LanceDB stores ONE fused vector per clip plus the tag
  vector; text queries embed with the same model so cosine similarity
  works across stores.
- **Optional dependency**: ``import lancedb`` — when missing, callers
  should fall back to the JSONL corpus (feature-detect with
  ``lancedb_available()``), never crash.
- **Manifest-first ingest**: the Open-Sora CSV (video id, path, caption)
  is read first; embedding happens in batches on GPU with resumability
  (rows already in the table are skipped).

Usage
-----
    from lib.corpus_lancedb import OpenSoraStore, lancedb_available

    if lancedb_available():
        store = OpenSoraStore("/kaggle/temp/openmontage/lancedb")
        store.ingest_manifest(rows)            # rows: [{video_id, path, caption, ...}]
        hits = store.rank_by_text("neon city rain at night", k=8)
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

EMBED_DIM = 512


def lancedb_available() -> bool:
    try:
        import lancedb  # noqa: F401
        return True
    except Exception:
        return False


@dataclass
class SoraClip:
    """One indexed Open-Sora clip (mirrors ClipRecord where sensible)."""
    clip_id: str
    local_path: str            # absolute or dataset-relative video path
    caption: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    aesthetic_score: float = 0.0
    motion_score: float = 0.0
    kind: str = "video"
    added_at: float = 0.0

    def to_metadata(self) -> dict[str, Any]:
        d = asdict(self)
        return d


class OpenSoraStore:
    """LanceDB table of CLIP embeddings over a local Open-Sora-Dataset."""

    TABLE = "clips"

    def __init__(self, db_path: str | Path):
        if not lancedb_available():
            raise RuntimeError(
                "lancedb is not installed. pip install lancedb "
                "(or use the JSONL corpus fallback in lib/corpus.py)"
            )
        import lancedb

        self.db_path = str(db_path)
        self.db = lancedb.connect(self.db_path)
        self._embedder = None  # lazy CLIP singleton

    # ------------------------------------------------------------------
    # Embedding (lazy, device-aware — CLIP runs on GPU when present)
    # ------------------------------------------------------------------
    @property
    def embedder(self):
        if self._embedder is None:
            from lib.clip_embedder import ClipEmbedder

            self._embedder = ClipEmbedder()
        return self._embedder

    # ------------------------------------------------------------------
    # Table management
    # ------------------------------------------------------------------
    def _table(self):
        try:
            return self.db.open_table(self.TABLE)
        except Exception:
            return None

    def count(self) -> int:
        t = self._table()
        try:
            return t.count_rows() if t else 0
        except Exception:
            return len(t.to_arrow()) if t else 0

    def existing_ids(self) -> set[str]:
        t = self._table()
        if t is None:
            return set()
        try:
            import pyarrow.compute as pc

            tbl = t.to_arrow()
            if "clip_id" in tbl.column_names:
                return set(tbl.column("clip_id").to_pylist())
            return set()
        except Exception:
            return set()

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------
    def ingest_manifest(
        self,
        rows: list[dict[str, Any]],
        *,
        batch_size: int = 64,
        frame_probe: bool = True,
        progress_every: int = 500,
    ) -> dict[str, Any]:
        """Embed + insert manifest rows.

        Each row needs at least ``local_path`` (existing file) or
        ``thumbnail`` (image path); ``clip_id``/``caption`` recommended.
        Rows whose clip_id already exists are skipped (resumable).

        ``frame_probe=True`` extracts one mid-frame per video with
        OpenCV/ffmpeg for the visual channel; when only a ``caption``
        exists the row is embedded text-only (weaker but valid).
        """
        import pyarrow as pa

        existing = self.existing_ids()
        pending = [r for r in rows if str(r.get("clip_id") or r.get("video_id") or r.get("local_path")) not in existing]
        added, skipped_text_only, errors = 0, 0, []

        t0 = time.time()
        for start in range(0, len(pending), batch_size):
            batch = pending[start : start + batch_size]
            vecs, metas = [], []
            for row in batch:
                try:
                    vec = self._embed_row(row, frame_probe=frame_probe)
                    if vec is None:
                        skipped_text_only += 1
                        continue
                    clip = SoraClip(
                        clip_id=str(row.get("clip_id") or row.get("video_id") or Path(row["local_path"]).stem),
                        local_path=str(row.get("local_path", "")),
                        caption=str(row.get("caption") or row.get("text") or ""),
                        duration=float(row.get("duration") or 0.0),
                        width=int(row.get("width") or 0),
                        height=int(row.get("height") or 0),
                        aesthetic_score=float(row.get("aesthetic_score") or 0.0),
                        motion_score=float(row.get("motion_score") or 0.0),
                        added_at=time.time(),
                    )
                    vecs.append(vec)
                    metas.append({**clip.to_metadata(), "vector": vec.tolist()})
                    added += 1
                except Exception as exc:  # one bad row must not kill the run
                    errors.append(f"{row.get('local_path', '?')}: {exc}")
            if metas:
                # LanceDB accepts a pyarrow Table (dict-of-columns alone is
                # read as a single row dict and rejected).
                vectors = [m.pop("vector") for m in metas]
                arrow = pa.table({
                    "vector": pa.array(vectors, type=pa.list_(pa.float32(), EMBED_DIM)),
                    **{k: pa.array([m[k] for m in metas]) for k in metas[0]},
                })
                t = self._table()
                if t is None:
                    self.db.create_table(self.TABLE, arrow, mode="create")
                else:
                    t.add(arrow)
            done = start + len(batch)
            if progress_every and done % progress_every < batch_size:
                print(f"[open-sora-index] {done}/{len(pending)} rows | added {added} | {time.time()-t0:.0f}s", flush=True)

        return {
            "rows_in": len(rows),
            "already_indexed": len(existing),
            "added": added,
            "text_only_skipped": skipped_text_only,
            "errors": errors[:20],
            "error_count": len(errors),
            "total_rows": self.count(),
            "seconds": round(time.time() - t0, 1),
        }

    def _embed_row(self, row: dict[str, Any], frame_probe: bool) -> Optional[np.ndarray]:
        """One 512-d vector for a row: mid-frame visual, caption-weighted.

        Fuses the visual channel (weight .7) and the text channel (.3)
        like the JSONL corpus' fused ranking, falling back to whichever
        channel exists.
        """
        visual = self._probe_visual(row, enabled=frame_probe)
        caption = str(row.get("caption") or row.get("text") or "")
        text_vec = self.embedder.embed_texts([caption])[0] if caption.strip() else None

        if visual is not None and text_vec is not None:
            v = 0.7 * visual + 0.3 * text_vec
        elif visual is not None:
            v = visual
        elif text_vec is not None:
            v = text_vec
        else:
            return None
        n = float(np.linalg.norm(v))
        return (v / n).astype(np.float32) if n > 0 else None

    def _probe_visual(self, row: dict[str, Any], enabled: bool) -> Optional[np.ndarray]:
        if not enabled:
            return None
        thumb = row.get("thumbnail")
        if thumb and Path(thumb).is_file():
            return self.embedder.embed_images([str(thumb)])[0]
        path = row.get("local_path")
        if path and Path(path).is_file() and str(path).lower().endswith((".mp4", ".mov", ".webm", ".avi", ".mkv")):
            mid = self._extract_midframe(str(path))
            if mid:
                try:
                    return self.embedder.embed_images([mid])[0]
                finally:
                    try:
                        Path(mid).unlink(missing_ok=True)
                    except Exception:
                        pass
        return None

    def _extract_midframe(self, video_path: str) -> Optional[str]:
        """One mid-frame JPEG via ffmpeg (no OpenCV dependency needed)."""
        import subprocess
        import tempfile

        # duration first
        try:
            dur_out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", video_path],
                capture_output=True, text=True, timeout=30,
            )
            duration = float(dur_out.stdout.strip() or 0)
        except Exception:
            duration = 0.0
        at = max(0.0, duration / 2) if duration > 0 else 1.0
        out = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        out.close()
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(at), "-i", video_path, "-frames:v", "1",
                 "-vf", "scale=336:-2", "-q:v", "4", out.name],
                capture_output=True, timeout=60,
            )
            if Path(out.name).stat().st_size > 0:
                return out.name
        except Exception:
            pass
        try:
            Path(out.name).unlink(missing_ok=True)
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Retrieval — same result shapes as lib/corpus.py
    # ------------------------------------------------------------------
    def rank_by_text(self, query: str, k: int = 8) -> list[dict[str, Any]]:
        """Embed the query, ANN-search, return hit dicts sorted by score."""
        qvec = self.embedder.embed_texts([query])[0]
        t = self._table()
        if t is None:
            return []
        try:
            res = t.search(qvec.tolist()).metric("cosine").limit(k).to_list()
        except Exception:
            try:
                res = t.search(qvec.tolist()).limit(k).to_list()
            except Exception:
                return []
        out = []
        for row in res:
            score = 1.0 - float(row.get("_distance", 0.0))  # cosine distance → sim
            out.append({
                "clip_id": row.get("clip_id", ""),
                "local_path": row.get("local_path", ""),
                "caption": row.get("caption", ""),
                "duration": float(row.get("duration") or 0.0),
                "width": int(row.get("width") or 0),
                "height": int(row.get("height") or 0),
                "aesthetic_score": float(row.get("aesthetic_score") or 0.0),
                "motion_score": float(row.get("motion_score") or 0.0),
                "score": score,
            })
        return out

    def knn(self, clip_id: str, k: int = 5) -> list[dict[str, Any]]:
        """Nearest neighbours of an indexed clip."""
        t = self._table()
        if t is None:
            return []
        try:
            tbl = t.to_arrow()
            ids = tbl.column("clip_id").to_pylist()
            if clip_id not in ids:
                return []
            vec = tbl.column("vector")[ids.index(clip_id)].as_py()
            res = t.search(vec).metric("cosine").limit(k + 1).to_list()
        except Exception:
            return []
        return [
            {**{f: r.get(f) for f in ("clip_id", "local_path", "caption", "duration")},
             "score": 1.0 - float(r.get("_distance", 0.0))}
            for r in res if r.get("clip_id") != clip_id
        ][:k]
