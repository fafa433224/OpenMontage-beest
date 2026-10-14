"""Collect Open-Sora b-roll as an isolated subprocess (own GPU assignment).

Runs OUTSIDE the main renderer process so it can own a specific GPU via
CUDA_VISIBLE_DEVICES while whisper transcription owns another — the dual-T4
Kaggle setup. Reads a JSON job, writes a JSON result; never imports the
renderer's state.

Job spec (stdin or --job file):
    {"queries": [{"q": "neon city rain", "n": 2}, ...],
     "dataset_dir": "/kaggle/temp/open-sora-dataset",   # optional
     "stage_dir": "remotion-composer/public/broll",      # where to copy
     "max_clips": 12}

Result (stdout JSON):
    {"entries": [{"path": "broll/os_001.mp4", "duration": 12.4}, ...],
     "queries_used": [...], "note": "..."}
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def duration_of(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True, help="path to the JSON job spec")
    ap.add_argument("--out", required=True, help="path for the JSON result")
    args = ap.parse_args()

    job = json.loads(Path(args.job).read_text())
    queries = job.get("queries") or []
    stage_dir = Path(job["stage_dir"])
    stage_dir.mkdir(parents=True, exist_ok=True)
    max_clips = int(job.get("max_clips", 12))

    result = {"entries": [], "queries_used": [], "note": "", "errors": []}

    repo_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo_root))

    try:
        from lib.corpus_lancedb import OpenSoraStore, lancedb_available

        dataset_dir = job.get("dataset_dir") or os.environ.get("OPEN_SORA_DIR", "")
        db = job.get("lancedb_dir") or (
            str(Path(dataset_dir) / "lancedb") if dataset_dir else ""
        ) or os.environ.get("OPEN_SORA_LANCEDB", "")

        if not lancedb_available() or not db or not Path(db).exists():
            result["note"] = "open-sora index unavailable — renderer falls back to synth b-roll"
            Path(args.out).write_text(json.dumps(result))
            return 0

        store = OpenSoraStore(db)
        seen = set()
        entries = []
        t0 = time.time()
        for qspec in queries:
            q = qspec.get("q", "")
            n = int(qspec.get("n", 2))
            if not q:
                continue
            try:
                hits = store.rank_by_text(q, k=n * 3)
            except Exception as exc:
                result["errors"].append(f"search {q!r}: {exc}")
                continue
            result["queries_used"].append(q)
            taken = 0
            for h in hits:
                if taken >= n or len(entries) >= max_clips:
                    break
                src = Path(h.get("local_path", ""))
                if not src.is_file() or h.get("clip_id") in seen:
                    continue
                dur = h.get("duration") or duration_of(src)
                if dur < 2.0:  # too short to loop under a cut
                    continue
                seen.add(h["clip_id"])
                staged = stage_dir / f"{h['clip_id']}{src.suffix or '.mp4'}"
                if not staged.exists():
                    shutil.copy2(src, staged)
                entries.append({
                    "path": f"broll/{staged.name}",  # relative to public/
                    "duration": round(dur, 2),
                    "score": round(h.get("score", 0.0), 3),
                    "caption": (h.get("caption") or "")[:120],
                })
                taken += 1
        result["entries"] = entries
        result["note"] = f"{len(entries)} open-sora clips staged in {time.time()-t0:.1f}s"
    except Exception as exc:
        result["note"] = f"open-sora collection failed ({exc}) — fallback to synth b-roll"
        result["errors"].append(str(exc))

    Path(args.out).write_text(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
