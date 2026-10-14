"""Open-Sora-Dataset indexer — build the LanceDB + CLIP search index.

The user-facing workflow (Kaggle T4 x2, 1 TB /kaggle/temp):

1. Download the dataset (or a curated slice of it) to $OPEN_SORA_DIR.
2. ``python tools/video/open_sora_index.py build`` — reads the CSV manifest,
   extracts one mid-frame per video (ffmpeg), embeds with CLIP (GPU 0 when
   available), writes the LanceDB table. Resumable: re-running skips rows
   already indexed.
3. From then on ``tools/video/stock_sources/open_sora_dataset.py`` serves
   semantic stock-footage search offline — replacing every scraper.

CLI (no tool-registry ceremony — this is a batch indexing job):
    python tools/video/open_sora_index.py build [--dataset-dir DIR] [--limit N] [--batch 64]
    python tools/video/open_sora_index.py search "neon city rain" [--k 8]
    python tools/video/open_sora_index.py status

Also usable as a module:
    from tools.video.open_sora_index import read_manifest, build_index, search
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.corpus_lancedb import OpenSoraStore, lancedb_available  # noqa: E402

# Manifest layouts the PKU-YuanGroup repo publishes (CSV headers differ by
# release; we match known column names and fall back to positional heuristics).
KNOWN_ID_COLS = ("video_id", "id", "clip_id", "videoid")
KNOWN_PATH_COLS = ("path", "video_path", "file", "video", "filename")
KNOWN_CAPTION_COLS = ("text", "caption", "description")


def _dataset_dir(explicit: Optional[str] = None) -> Path:
    import os
    if explicit:
        return Path(explicit)
    env = (os.environ.get("OPEN_SORA_DIR") or "").strip()
    return Path(env) if env else Path("/kaggle/temp/open-sora-dataset")


def find_manifest(dataset_dir: Path) -> Optional[Path]:
    """Locate the CSV/JSON manifest inside a downloaded dataset."""
    if not dataset_dir.is_dir():
        return None
    candidates: list[Path] = []
    for pattern in ("*.csv", "*.json", "**/*.csv", "**/*.json"):
        candidates.extend(sorted(dataset_dir.glob(pattern)))
    # Prefer files with 'sora'/'meta'/'caption'/'cap' in the name, then any.
    candidates.sort(key=lambda p: (
        0 if any(t in p.name.lower() for t in ("sora", "meta", "caption", "cap")) else 1,
        len(str(p)),
    ))
    # Skip our own pointer file.
    candidates = [c for c in candidates if c.name != "lancedb.json"]
    return candidates[0] if candidates else None


def _rows_from_json(path: Path, dataset_dir: Path, limit: Optional[int]) -> list[dict[str, Any]]:
    """Read a JSON manifest — handles both {path: caption} dicts and
    [{path|video|url, caption|text}] lists (the HF Open-Sora-Plan layout
    ships `llava_path_cap_*.json` as a dict)."""
    import json as _json

    data = _json.loads(path.read_text(encoding="utf-8", errors="replace"))
    pairs: list[tuple[str, str, str]] = []  # (video_id, rel_path, caption)

    if isinstance(data, dict):
        for key, value in data.items():
            caption = value if isinstance(value, str) else (
                (value or {}).get("caption") or (value or {}).get("text") or ""
            )
            rel = key
            # HF keys may be full URLs or absolute paths — reduce to basename.
            if rel.startswith(("http://", "https://")):
                rel = rel.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            elif "/" in rel:
                rel = rel.rsplit("/", 1)[-1]
            pairs.append((Path(rel).stem, rel, caption))
    elif isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            rel = str(item.get("path") or item.get("video") or item.get("url") or item.get("id") or "")
            caption = str(item.get("caption") or item.get("text") or "")
            if rel.startswith(("http://", "https://")):
                rel = rel.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            elif "/" in rel:
                rel = rel.rsplit("/", 1)[-1]
            pairs.append((Path(rel).stem, rel, caption))

    # Map basenames to actual files under the dataset dir (any subdir depth).
    files: dict[str, Path] = {}
    for ext in ("*.mp4", "*.mov", "*.webm", "*.avi", "*.mkv"):
        for f in dataset_dir.rglob(ext):
            files.setdefault(f.name, f)

    rows: list[dict[str, Any]] = []
    for vid, rel, caption in pairs:
        f = files.get(rel)
        if f is None:
            # try with a different container suffix
            stem = Path(rel).stem
            f = next((files[n] for n, p in files.items() if p.stem == stem), None)
        if f is None:
            continue
        rows.append({
            "clip_id": f"open_sora_{vid}",
            "local_path": str(f),
            "caption": caption,
        })
        if limit and len(rows) >= limit:
            break
    return rows


def read_manifest(dataset_dir: Path, limit: Optional[int] = None) -> list[dict[str, Any]]:
    """Read the CSV/JSON manifest into row dicts with normalised keys.

    Resolves each row's video path against the dataset dir (JSON basenames
    are matched against files at any depth) and drops rows whose file is
    missing, so the indexer only ever sees real footage.
    """
    manifest = find_manifest(dataset_dir)
    if manifest is None:
        raise FileNotFoundError(
            f"No CSV/JSON manifest found under {dataset_dir}. Point OPEN_SORA_DIR at "
            "the dataset root (the directory containing the manifest and the videos)."
        )
    if manifest.suffix.lower() == ".json":
        rows = _rows_from_json(manifest, dataset_dir, limit)
        if not rows:
            raise ValueError(
                f"JSON manifest {manifest} produced no resolvable video rows."
            )
        return rows

    rows: list[dict[str, Any]] = []
    with open(manifest, encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        id_col = next((c for c in KNOWN_ID_COLS if c in headers), None)
        path_col = next((c for c in KNOWN_PATH_COLS if c in headers), None)
        caption_col = next((c for c in KNOWN_CAPTION_COLS if c in headers), None)
        for raw in reader:
            path_val = (path_col and raw.get(path_col)) or ""
            caption = (caption_col and raw.get(caption_col)) or ""
            vid = (id_col and raw.get(id_col)) or Path(path_val).stem
            if not path_val:
                continue
            # resolve against dataset root / common subdirs
            cand = dataset_dir / path_val
            if not cand.is_file():
                for sub in ("videos", "data", "video"):
                    alt = dataset_dir / sub / path_val
                    if alt.is_file():
                        cand = alt
                        break
            if not cand.is_file():
                continue
            rows.append({
                "clip_id": f"open_sora_{vid}",
                "local_path": str(cand),
                "caption": caption,
            })
            if limit and len(rows) >= limit:
                break
    return rows


def build_index(
    dataset_dir: Path,
    *,
    limit: Optional[int] = None,
    batch: int = 64,
    db_dir: Optional[Path] = None,
) -> dict[str, Any]:
    if not lancedb_available():
        raise RuntimeError("pip install lancedb first (requirements-gpu.txt has it).")
    rows = read_manifest(dataset_dir, limit=limit)
    print(f"[open-sora-index] manifest rows with files: {len(rows)}")
    db_path = db_dir or (dataset_dir / "lancedb")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = OpenSoraStore(db_path)
    stats = store.ingest_manifest(rows, batch_size=batch)
    # persist a small pointer file so the source adapter can auto-discover
    (dataset_dir / "lancedb.json").write_text(json.dumps({"db": str(db_path), "rows": stats["total_rows"]}))
    return stats


def search(query: str, k: int = 8, dataset_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    root = dataset_dir or _dataset_dir()
    db = root / "lancedb"
    store = OpenSoraStore(db)
    return store.rank_by_text(query, k=k)


def status(dataset_dir: Optional[Path] = None) -> dict[str, Any]:
    root = dataset_dir or _dataset_dir()
    db = root / "lancedb"
    if not lancedb_available():
        return {"lancedb": False, "db": str(db)}
    if not db.exists():
        return {"lancedb": True, "indexed": 0, "db": str(db)}
    store = OpenSoraStore(db)
    manifest = find_manifest(root)
    return {
        "lancedb": True,
        "db": str(db),
        "indexed": store.count(),
        "manifest": str(manifest) if manifest else None,
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["build", "search", "status"])
    ap.add_argument("query", nargs="?", help="search query (for 'search')")
    ap.add_argument("--dataset-dir", default=None)
    ap.add_argument("--limit", type=int, default=None, help="cap manifest rows (smoke tests)")
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--k", type=int, default=8)
    args = ap.parse_args(argv)

    root = _dataset_dir(args.dataset_dir)

    if args.command == "status":
        print(json.dumps(status(root), indent=2))
        return 0
    if args.command == "search":
        if not args.query:
            ap.error("search requires a query")
        t0 = time.time()
        hits = search(args.query, k=args.k, dataset_dir=root)
        dt = (time.time() - t0) * 1000
        for h in hits:
            print(f"{h['score']:.3f}  {h['duration']:6.1f}s  {h['clip_id'][:34]:34}  {h['caption'][:60]}")
            print(f"        {h['local_path']}")
        print(f"-- {len(hits)} hits in {dt:.0f} ms")
        return 0
    if args.command == "build":
        stats = build_index(root, limit=args.limit, batch=args.batch)
        print(json.dumps(stats, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
