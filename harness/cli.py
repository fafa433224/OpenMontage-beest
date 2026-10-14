"""Harness CLI — run / resume / status / channels / doctor."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _cmd_run(args: argparse.Namespace) -> int:
    from harness.runner import run_project

    summary = run_project(
        Path(args.input),
        project_id=args.project,
        channel=args.channel,
        out_dir=Path(args.out) if args.out else None,
        motion=args.motion,
        approval=args.approval,
        duration_hint_s=args.duration,
        concurrency=args.concurrency,
        broll_source=args.broll_source,
        no_llm=args.no_llm,
        single_gpu=args.single_gpu,
    )
    status = summary.get("status")
    if status == "awaiting_approval":
        return 0
    return 0 if status == "complete" else 2


def _cmd_resume(args: argparse.Namespace) -> int:
    from harness.runner import resume_project

    summary = resume_project(
        args.project,
        channel=args.channel,
        motion=args.motion,
        concurrency=args.concurrency,
        broll_source=args.broll_source,
        no_llm=args.no_llm,
        single_gpu=args.single_gpu,
        out_dir=Path(args.out) if args.out else None,
    )
    return 0 if summary.get("status") == "complete" else 2


def _cmd_status(args: argparse.Namespace) -> int:
    from harness.runner import project_status

    print(json.dumps(project_status(args.project), indent=1))
    return 0


def _cmd_channels(_: argparse.Namespace) -> int:
    from lib.brand_profile import list_channels

    from lib.brand_profile import load_brand

    for channel_id in list_channels():
        try:
            brand = load_brand(channel_id)
            print(f"{channel_id:16s} {brand.name:20s} caption={brand.caption_style:12s} "
                  f"pacing={brand.pacing:9s} fonts={brand.font_pairing}")
        except Exception as exc:
            print(f"{channel_id:16s} ERROR: {exc}")
    return 0


def _cmd_doctor(_: argparse.Namespace) -> int:
    """Preflight: everything the stack needs, checked in dependency order."""
    from lib.llm_client import llm_status

    checks: list[tuple[bool, str, str]] = []

    checks.append((sys.version_info >= (3, 10), "python>=3.10", sys.version.split()[0]))

    for mod, label in [("yaml", "pyyaml"), ("jsonschema", "jsonschema"),
                       ("numpy", "numpy"), ("PIL", "Pillow"), ("scipy", "scipy")]:
        try:
            __import__(mod)
            checks.append((True, f"python:{label}", "ok"))
        except Exception:
            checks.append((False, f"python:{label}", "missing (pip install -r requirements.txt)"))

    try:
        import librosa  # noqa: F401

        checks.append((True, "python:librosa", "ok"))
    except Exception:
        checks.append((False, "python:librosa", "missing (beat sync only)"))

    for binary, label, fix in [
        ("ffmpeg", "ffmpeg", "https://ffmpeg.org"),
        ("ffprobe", "ffprobe", "ships with ffmpeg"),
        ("node", "node>=22", "https://nodejs.org (Remotion 4 / HyperFrames)"),
        ("npx", "npx", "ships with node"),
    ]:
        path = shutil.which(binary)
        version = ""
        if path:
            if binary == "node":
                out = shutil.which("node") and _run(f"{path} --version")
                version = (out or "").strip().lstrip("v")
                checks.append((version.split(".")[0].isdigit() and int(version.split(".")[0]) >= 22,
                               label, version or "found"))
                continue
            checks.append((True, label, path))
        else:
            checks.append((False, label, fix))

    try:
        import torch  # noqa: F401

        gpu = torch.cuda.is_available()
        gpus = torch.cuda.device_count() if gpu else 0
        checks.append((True, "torch", f"{gpus} cuda gpus" if gpu else "cpu-only (slower transcribe)"))
    except Exception:
        checks.append((True, "torch", "not installed (CPU transcription will fail — see requirements-gpu.txt)"))

    try:
        import faster_whisper  # noqa: F401

        checks.append((True, "faster-whisper", "ok"))
    except Exception:
        checks.append((False, "faster-whisper", "missing (pip install -r requirements-gpu.txt)"))

    try:
        import piper  # noqa: F401

        checks.append((True, "piper-tts", "ok (free offline narration)"))
    except Exception:
        checks.append((True, "piper-tts", "not installed (supply a voiceover file, or pip install piper-tts)"))

    try:
        import lancedb  # noqa: F401

        checks.append((True, "lancedb", "ok (Open-Sora corpus)"))
    except Exception:
        checks.append((True, "lancedb", "optional — real-footage b-roll disabled"))

    status = llm_status()
    checks.append((bool(status.get("available")), "runtime-llm",
                   f"{status.get('provider')}/{status.get('model')}"
                   if status.get("available")
                   else "not configured (seeded director + heuristics; set LLM_* or a provider key)"))

    open_sora = __import__("os").environ.get("OPEN_SORA_DIR", "")
    checks.append((bool(open_sora), "OPEN_SORA_DIR",
                   open_sora or "not set (synth b-roll fallback)"))

    from lib.brand_profile import list_channels

    channels = list_channels()
    checks.append((len(channels) >= 1, "channels", ", ".join(channels)))

    composer = REPO_ROOT / "remotion-composer"
    checks.append(((composer / "package.json").exists(), "remotion-composer", "present"))
    checks.append(((composer / "node_modules").exists(), "node_modules",
                   "present" if (composer / "node_modules").exists()
                   else "run: cd remotion-composer && npm ci"))

    width = max(len(c[1]) for c in checks) + 2
    failures = 0
    print("OpenMontage harness — doctor\n" + "-" * 60)
    for ok, label, detail in checks:
        mark = "OK " if ok else "!! "
        if not ok:
            failures += 1
        print(f"{mark}{label.ljust(width)}{detail}")
    print("-" * 60)
    print(f"{len(checks) - failures}/{len(checks)} checks passed")
    return 1 if failures else 0


def _run(cmd: str) -> str:
    import subprocess

    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="harness",
        description="OpenMontage headless production harness — "
                    "script/voiceover/idea -> finished, QC-passed video.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="produce a video from an input")
    run.add_argument("input", help="script.json | *.txt/*.md | voiceover audio | "
                                   "input dir with script.json+voiceover.mp3 | bare idea text")
    run.add_argument("--project", default=None, help="project id (default: derived from input name)")
    run.add_argument("--channel", default="default", help="channel brand (see: harness channels)")
    run.add_argument("--out", default=None, help="output dir (default projects/<id>/renders)")
    run.add_argument("--motion", choices=["template", "code2motion", "auto"], default="auto",
                     help="intro/outro authoring mode")
    run.add_argument("--approval", choices=["auto", "gate", "ask"], default="auto",
                     help="auto: headless pre-authorization; gate: freeze plan for editing; ask: prompt")
    run.add_argument("--duration", type=float, default=None,
                     help="target narration seconds (shapes the LLM plan)")
    run.add_argument("--concurrency", type=int, default=None, help="Remotion render tabs")
    run.add_argument("--broll-source", choices=["auto", "synth", "open-sora"], default="auto")
    run.add_argument("--no-llm", action="store_true", help="seeded director only")
    run.add_argument("--single-gpu", action="store_true", help="pin everything to GPU 0")
    run.set_defaults(func=_cmd_run)

    resume = sub.add_parser("resume", help="continue a checkpointed project")
    resume.add_argument("project")
    resume.add_argument("--channel", default=None)
    resume.add_argument("--motion", choices=["template", "code2motion", "auto"], default=None)
    resume.add_argument("--out", default=None)
    resume.add_argument("--concurrency", type=int, default=None)
    resume.add_argument("--broll-source", choices=["auto", "synth", "open-sora"], default=None)
    resume.add_argument("--no-llm", action="store_true")
    resume.add_argument("--single-gpu", action="store_true")
    resume.set_defaults(func=_cmd_resume)

    status = sub.add_parser("status", help="show a project's harness state")
    status.add_argument("project")
    status.set_defaults(func=_cmd_status)

    channels = sub.add_parser("channels", help="list channel brands")
    channels.set_defaults(func=_cmd_channels)

    doctor = sub.add_parser("doctor", help="preflight every dependency")
    doctor.set_defaults(func=_cmd_doctor)

    args = ap.parse_args()
    return args.func(args)
