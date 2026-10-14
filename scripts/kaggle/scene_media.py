"""Bake per-scene b-roll clips so the renderer never has to loop or trim.

Why this module exists
----------------------
The first compiled-video implementation handed `OffthreadVideo` a source
path, a trim-in, a trim-out and wrapped it in `<Loop>` so a 24 s scene over
an 8 s clip would not freeze on the last frame. That construct is correct
*and* racy: Remotion's `--concurrency` workers share one extracted-asset
directory, several of them read the same file at different loop phases, and
the compositor intermittently fails outright —

    Compositor error: No frame found at position 64426 for source
    .../remotion-assets-dir/9229182759299874.mp4

Measured on a 2190-frame render: at `--concurrency=2` the same props and the
same command failed on one run and passed on the next; at `--concurrency=1`
it passed every time. A race that costs a 20-minute Kaggle session at random
is not something to retry around when it can be removed.

So the loop is resolved at PREP time instead of render time. For each scene
that needs footage we write one clip that is exactly as long as the scene:

  * the source window is played, not re-derived per frame;
  * long scenes get up to 2x ambient slow-motion first (what an editor would
    do), and only the remainder is repeated;
  * the renderer then reads a plain file start-to-finish — no `<Loop>`, no
    `trimBefore`, no shared-phase seeks, nothing to race on.

Cost: one ffmpeg pass per hero scene (a second or two each), and a few extra
megabytes of intermediates.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

#: Slow a clip by at most this much before falling back to repetition.
MAX_SLOWDOWN = 2.0
#: Output frame rate for prepared clips. Matches the composer's 30 fps so the
#: renderer never has to resample.
TARGET_FPS = 30

#: Remotion's compositor addresses video in a fixed 1/12800 timescale — the
#: value it reports in "No frame found at position N", and the time_base of
#: every clip the composer ships. ffmpeg picks a timescale from the output
#: frame rate instead, so a 30 fps bake came out as 1/15360 with
#: ``start_time=0.033``. The compositor then mapped a request for 3.36 s onto
#: position 43008 (= 3.36 x 12800), which does not exist on a 15360-based
#: timeline, and the render died with "No frame found at position 43008".
#: Pinning the timescale is what makes a baked clip addressable.
COMPOSITOR_TIMESCALE = 12800
#: Never prepare a clip for a scene longer than this; something is wrong.
MAX_SCENE_SECONDS = 900.0


def _slug(text: str) -> str:
    """Filesystem-safe, stable name for a project id."""
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in str(text or "")]
    out = "".join(keep).strip("-")
    return out or "project"


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float((out.stdout or "0").strip())
    except ValueError:
        return 0.0


def _run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed ({r.returncode}): {' '.join(cmd[:6])}…\n{(r.stderr or '')[-500:]}"
        )


def bake_scene_clip(
    source: Path,
    out: Path,
    scene_seconds: float,
    start_seconds: float,
    source_seconds: Optional[float] = None,
    fps: int = TARGET_FPS,
) -> dict[str, Any]:
    """Write ``out`` as a clip of exactly ``scene_seconds`` taken from ``source``.

    Returns the metadata the renderer needs (and the numbers behind the
    choice, so a viewer can tell slow-mo from repetition after the fact).
    """
    if scene_seconds <= 0:
        raise ValueError("scene_seconds must be > 0")
    if scene_seconds > MAX_SCENE_SECONDS:
        raise ValueError(f"scene_seconds {scene_seconds} is implausible")

    total = source_seconds if source_seconds else _probe_duration(source)
    if total <= 0:
        raise RuntimeError(f"cannot determine duration of {source}")

    start = max(0.0, min(float(start_seconds), max(0.0, total - 0.25)))
    window = max(0.25, total - start)

    # Slow the clip toward the scene length, but no further than MAX_SLOWDOWN.
    slowdown = min(MAX_SLOWDOWN, max(1.0, scene_seconds / window))
    stretched = window * slowdown
    loops = 0 if stretched >= scene_seconds else int(scene_seconds / stretched) + 1

    # Take only as much source as the scene actually needs. Cutting the whole
    # window and relying on the sequence to clip it produced 6.27s files for
    # 3.5s scenes — harmless to the render, but it means the baked clip is not
    # the length it claims, which is exactly what the renderer checks to
    # decide it can skip its loop/trim logic.
    needed = min(window, scene_seconds / slowdown)

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".window.mp4")

    # 1) Cut the window out of the source at the output frame rate.
    #    `setpts=PTS-STARTPTS` puts the first frame at 0 (a short input seek
    #    otherwise leaves a 0.033 s offset, which the compositor counts as a
    #    missing opening frame).
    pts = f"PTS*{slowdown:.6f}" if slowdown > 1.001 else "PTS"
    vf_cut = f"setpts={pts}-STARTPTS,fps={fps}"
    _run([_ffmpeg(), "-y", "-v", "error",
          "-ss", f"{start:.3f}", "-t", f"{needed:.3f}", "-i", str(source),
          "-an", "-vf", vf_cut, "-c:v", "libx264", "-preset", "veryfast",
          "-crf", "18", "-pix_fmt", "yuv420p",
          # Every frame independently decodable (no GOP-relative seeking) and
          # on the compositor's own timescale.
          "-g", "1", "-keyint_min", "1", "-sc_threshold", "0", "-bf", "0",
          "-video_track_timescale", str(COMPOSITOR_TIMESCALE),
          str(tmp)])

    # 2) Repeat it if it still does not cover the scene, then trim exactly.
    if loops <= 0:
        tmp.replace(out)
    else:
        _run([_ffmpeg(), "-y", "-v", "error",
              "-stream_loop", "-1", "-i", str(tmp),
              "-t", f"{scene_seconds:.3f}", "-an",
              "-vf", f"setpts=PTS-STARTPTS,fps={fps}", "-c:v", "libx264",
              "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
              "-g", "1", "-keyint_min", "1", "-sc_threshold", "0", "-bf", "0",
              "-video_track_timescale", str(COMPOSITOR_TIMESCALE),
              str(out)])
        tmp.unlink(missing_ok=True)

    return {
        "prepared": True,
        "sceneSeconds": round(scene_seconds, 3),
        "sourceWindow": round(needed, 3),
        "slowdown": round(slowdown, 3),
        "loops": loops,
    }


def prepare_scene_media(
    props: dict[str, Any],
    composer: Path,
    project_id: str = "scene-media",
    verbose: bool = True,
) -> dict[str, Any]:
    """Rewrite every compiled scene's media into a pre-baked, exact-length clip.

    Mutates ``props`` in place and returns a small report. Scenes whose
    footage cannot be prepared are left on the render-time path (and the
    caller's retry ladder still covers them) rather than failing the render.
    """
    public = composer / "public"
    # Clips are namespaced by project. Cut ids ("s1-hero") repeat across
    # projects, so a flat directory let one render read another project's
    # footage — a wrong-footage bug that is invisible at a glance because the
    # filename looks right. Clear the project's own directory, never a
    # sibling's.
    cut_dir = public / "broll" / "scenes" / _slug(project_id)
    if cut_dir.exists():
        for stale in cut_dir.glob("*.mp4"):
            stale.unlink(missing_ok=True)
    report: dict[str, Any] = {"prepared": [], "skipped": [], "seconds": 0.0,
                              "dir": str(cut_dir.relative_to(public))}

    for i, cut in enumerate(props.get("cuts") or []):
        spec = cut.get("bespoke") if isinstance(cut, dict) else None
        media = (spec or {}).get("media")
        if not media or media.get("kind") != "video":
            continue
        try:
            scene_seconds = float(cut["out_seconds"]) - float(cut["in_seconds"])
        except (KeyError, TypeError, ValueError):
            report["skipped"].append({"cut": cut.get("id"), "why": "no span"})
            continue

        source = public / str(media["src"]).lstrip("/")
        if not source.exists():
            report["skipped"].append({"cut": cut.get("id"), "why": f"missing {source}"})
            continue

        out = cut_dir / f"{cut.get('id', i)}.mp4"
        try:
            meta = bake_scene_clip(
                source, out, scene_seconds,
                float(media.get("startSeconds") or 0.0),
                media.get("sourceDuration"),
            )
        except Exception as exc:  # noqa: BLE001 — one bad clip must not lose the run
            report["skipped"].append({"cut": cut.get("id"), "why": f"{type(exc).__name__}: {exc}"})
            if verbose:
                print(f"  ! could not pre-bake {cut.get('id')}: {exc}", flush=True)
            continue

        media.update({
            "src": f"broll/scenes/{_slug(project_id)}/{out.name}",
            "kind": "video",
            "startSeconds": 0.0,
            "sourceDuration": meta["sceneSeconds"],
            "prepared": True,
        })
        report["prepared"].append({"cut": cut.get("id"), **meta})
        report["seconds"] += meta["sceneSeconds"]
        if verbose:
            mode = (f"{meta['slowdown']:.2f}x slow-mo" if meta["slowdown"] > 1.001
                    else "straight")
            extra = f" + {meta['loops']} repeat(s)" if meta["loops"] else ""
            print(f"  baked {cut.get('id')}: {meta['sceneSeconds']:.1f}s from "
                  f"{meta['sourceWindow']:.1f}s ({mode}{extra})", flush=True)

    return report
