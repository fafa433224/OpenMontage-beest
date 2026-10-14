"""The harness runner — PLAN -> EXECUTE -> REVIEW with checkpoints.

Stage machine (each stage checkpoints, so ``harness resume`` re-enters):

    1 input      detect input type, derive script.json (LLM plan/heuristics/
                 verbatim/transcript)
    2 narration  piper TTS per section + voiceover.mp3 (skipped when the
                 input already carries audio) OR voiceover-first cuts
    3 render     delegate to the proven production renderer
                 (scripts/kaggle/render_voiceover_video.py) which runs
                 whisper on GPU 0, Open-Sora CLIP on GPU 1, builds branded
                 props, renders the Remotion body + HyperFrames
                 intro/outro, concatenates and QCs
    4 review     QC tripwires + optional LLM review -> verdict report

Approval policies: ``auto`` (headless default — a pre-authorization
decision is recorded), ``gate`` (freeze the plan to
``projects/<id>/plan-awaiting-approval.json``, exit 0, resume after
editing), ``ask`` (interactive prompt).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
KAGGLE_SCRIPTS = REPO_ROOT / "scripts" / "kaggle"
if str(KAGGLE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(KAGGLE_SCRIPTS))

from harness import inputs as harness_inputs  # noqa: E402
from harness import narration as harness_narration  # noqa: E402
from lib.brand_profile import BrandProfile, list_channels, load_brand  # noqa: E402
from lib.llm_client import LLMClient  # noqa: E402

STAGES = ["input", "narration", "render", "review"]


class HarnessError(RuntimeError):
    """Harness-level failure with a human-readable fix."""


# ---------------------------------------------------------------------------
# Project state (checkpoint per stage — crash recovery)
# ---------------------------------------------------------------------------


def _projects_dir() -> Path:
    from lib.paths import PROJECTS_DIR

    return Path(PROJECTS_DIR)


def _state_path(project_id: str) -> Path:
    return _projects_dir() / project_id / "harness-state.json"


def _load_state(project_id: str) -> dict[str, Any]:
    path = _state_path(project_id)
    if path.exists():
        return json.loads(path.read_text())
    return {"project_id": project_id, "completed": [], "data": {}}


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path(state["project_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1))
    # Also keep the canonical project.json compatible artifacts informed.
    events = path.parent / "events.jsonl"
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    with events.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": stamp, "kind": "harness",
                             "stage_done": state["completed"][-1] if state["completed"] else None,
                             "source": "harness"}) + "\n")


def _mark(state: dict[str, Any], stage: str, **data) -> None:
    if stage not in state["completed"]:
        state["completed"].append(stage)
    state["data"].update(data)
    _save_state(state)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _detect_gpus() -> list[str]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return [line.split(",")[0].strip() for line in out.splitlines() if line.strip()]
    except Exception:
        return []


def _llm() -> Optional[LLMClient]:
    try:
        client = LLMClient()
        return client if client.available else None
    except Exception:
        return None


def _new_project_id(name_hint: str) -> str:
    slug = "".join(ch if ch.isalnum() else "-" for ch in name_hint.lower()).strip("-")
    slug = slug[:40] or "video"
    base = _projects_dir()
    candidate, n = slug, 1
    while (base / candidate).exists():  # any existing project dir claims the id
        n += 1
        candidate = f"{slug}-{n}"
    return candidate


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


def _record_approval(project_id: str, policy: str) -> None:
    """Write the pre-authorization decision (the checkpoint-protocol way)."""
    decision_path = _projects_dir() / project_id / "decision_log.json"
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "category": "approval_policy",
        "decision": "full_run_preauthorized",
        "value": policy,
        "reason": "headless harness run (--approval)",
        "decided_by": "harness",
    }
    logs = []
    if decision_path.exists():
        try:
            logs = json.loads(decision_path.read_text())
            if isinstance(logs, dict):
                logs = logs.get("decisions", [])
        except Exception:
            logs = []
    logs.append(entry)
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    decision_path.write_text(json.dumps(logs, indent=1))


def _gate_plan(project_id: str, script: dict) -> None:
    gate_path = _projects_dir() / project_id / "plan-awaiting-approval.json"
    gate_path.parent.mkdir(parents=True, exist_ok=True)
    gate_path.write_text(json.dumps(script, indent=1, ensure_ascii=False))


# ---------------------------------------------------------------------------
# The stage machine
# ---------------------------------------------------------------------------


def run_project(
    input_path: Path,
    *,
    project_id: Optional[str] = None,
    channel: str = "default",
    out_dir: Optional[Path] = None,
    motion: str = "auto",
    approval: str = "auto",
    duration_hint_s: Optional[float] = None,
    concurrency: Optional[int] = None,
    broll_source: str = "auto",
    no_llm: bool = False,
    single_gpu: bool = False,
    render_extra_args: Optional[list[str]] = None,
    fast_visual: bool = False,
    skip_visual: bool = False,
    vision_review: bool = True,
) -> dict[str, Any]:
    """One command: input → finished video. Returns the summary dict."""
    t0 = time.time()
    input_path = Path(input_path).resolve()
    if project_id is None:
        project_id = _new_project_id(input_path.stem or "video")
    state = _load_state(project_id)
    state["channel"] = channel
    state["motion"] = motion

    work = _projects_dir() / project_id / "input"
    work.mkdir(parents=True, exist_ok=True)
    out = Path(out_dir) if out_dir else (_projects_dir() / project_id / "renders")
    out.mkdir(parents=True, exist_ok=True)

    brand: BrandProfile = load_brand(channel)
    llm = None if no_llm else _llm()
    gpus = _detect_gpus()
    print(f"[harness] project={project_id} channel={brand.channel_id} "
          f"llm={'on' if llm else 'seeded-only'} gpus={gpus or 'cpu'}", flush=True)

    # ---------------- stage: input ----------------
    if "input" not in state["completed"]:
        kind = harness_inputs.detect_input(input_path)
        print(f"[harness] input kind: {kind}", flush=True)
        voiceover_src: Optional[Path] = None

        if kind in ("script_dir",):
            src_dir = input_path
            script = harness_inputs.normalize_script(
                json.loads((src_dir / "script.json").read_text())
            )
            script["source"] = "user_script_dir"
            voiceover_src = (src_dir / "voiceover.mp3")
            for mp3 in (src_dir / "sections").glob("*.mp3"):
                shutil.copy(mp3, work / "sections" / mp3.name)
        elif kind == "script_json":
            script = harness_inputs.normalize_script(json.loads(input_path.read_text()))
            script["source"] = "user_script"
        elif kind == "audio_dir":
            voiceover_src = next(iter(sorted((input_path / "sections").glob("*.mp3")))).parent.parent
            # audio dir without script.json: treat the first audio as the voiceover
            audio_files = sorted(
                p for p in input_path.rglob("*") if p.suffix.lower() in harness_inputs.AUDIO_EXTENSIONS
            )
            if not audio_files:
                raise HarnessError("audio dir contains no audio files")
            voiceover_src = audio_files[0]
            kind = "audio"
        elif kind == "audio":
            voiceover_src = input_path
        elif kind == "text":
            script = harness_inputs.script_from_text(input_path, llm, duration_hint_s)
        else:  # idea
            script = harness_inputs.script_from_idea(
                input_path.read_text(encoding="utf-8", errors="replace").strip(),
                llm, duration_hint_s,
            )

        if kind == "audio" and voiceover_src is not None:
            # Voiceover-first: transcript drives the script + section cuts.
            words = harness_narration.transcribe_words(
                voiceover_src, work, gpu_id=(gpus[0] if gpus and not single_gpu else None)
            )
            derived = harness_inputs.script_from_transcript(
                words, voiceover_src, llm, work
            )
            script = derived["script"]
            harness_inputs.cut_section_audio(
                voiceover_src, derived["section_cuts"], work / "sections"
            )
            # The whole voiceover is the narration track.
            shutil.copy(voiceover_src, work / "voiceover.mp3")

        (work / "script.json").write_text(json.dumps(script, indent=1, ensure_ascii=False))
        print(f"[harness] plan ready ({script.get('source')}): "
              f"{len(script['sections'])} sections, "
              f"moods={script.get('broll_moods', [])[:3]}", flush=True)

        if approval == "gate":
            _gate_plan(project_id, script)
            _record_approval(project_id, "gate")
            print(f"[harness] plan frozen for approval at "
                  f"{_projects_dir() / project_id / 'plan-awaiting-approval.json'}\n"
                  f"       edit it, then: python -m harness resume {project_id}",
                  flush=True)
            return {"project_id": project_id, "status": "awaiting_approval"}
        _record_approval(project_id, approval)
        _mark(state, "input", script_source=script.get("source", "user"))
    else:
        script = json.loads((work / "script.json").read_text())
        print(f"[harness] resuming after input: {len(script['sections'])} sections", flush=True)

    # ---------------- stage: narration ----------------
    if "narration" not in state["completed"]:
        have_voiceover = (work / "voiceover.mp3").exists()
        if not have_voiceover:
            if script.get("source") in ("voiceover_transcript",):
                raise HarnessError("voiceover-first run lost its voiceover file")
            print("[harness] synthesizing narration (piper, offline, free)", flush=True)
            harness_narration.synthesize_narration(
                script, work,
                voices_dir=Path(os.environ.get("PIPER_VOICE_DIR", work.parent / "piper-voices")),
            )
        dur = harness_inputs.audio_duration(work / "voiceover.mp3")
        print(f"[harness] voiceover: {dur:.1f}s", flush=True)
        _mark(state, "narration", voiceover_seconds=dur)
    else:
        print(f"[harness] narration cached ({state['data'].get('voiceover_seconds', 0):.1f}s)",
              flush=True)

    # ---------------- stage: render (delegated, proven) ----------------
    name = "final"
    final_mp4 = out / f"{name}.mp4"
    if "render" not in state["completed" ] or not final_mp4.exists():
        render_cmd = [
            sys.executable, str(KAGGLE_SCRIPTS / "render_voiceover_video.py"),
            name, str(work), str(REPO_ROOT), str(out),
            "--channel", channel,
            "--motion", motion,
            "--broll-source", broll_source,
        ]
        # Pass through when asked for. The render stage handles the default
        # itself (Remotion's CPU-count default) and retries single-worker if
        # it hits the compositor frame race, which only affects media still
        # going through OffthreadVideo.
        if concurrency:
            render_cmd += ["--concurrency", str(concurrency)]
        if no_llm or llm is None:
            render_cmd += ["--no-llm"]
        if single_gpu:
            render_cmd += ["--single-gpu"]
        if render_extra_args:
            render_cmd += render_extra_args
        print(f"[harness] render: {' '.join(render_cmd[1:])}", flush=True)
        log_path = out / "render.log"
        with log_path.open("w") as log:
            proc = subprocess.run(render_cmd, stdout=log, stderr=subprocess.STDOUT)
        if proc.returncode != 0:
            tail = log_path.read_text(errors="replace")[-3000:]
            raise HarnessError(f"render stage failed (rc={proc.returncode}):\n{tail}")
        if not final_mp4.exists():
            raise HarnessError("render stage reported success but no final mp4")
        _mark(state, "render", final=str(final_mp4))
    else:
        print(f"[harness] render cached: {final_mp4}", flush=True)

    # ---------------- stage: review ----------------
    from harness.review import review_project

    verdict = review_project(
        project_id=project_id, out_dir=out, name=name, script=script,
        llm=llm, final=final_mp4,
        skip_visual=skip_visual,
        vision_enabled=vision_review,
        fast_visual=fast_visual,
    )
    _mark(state, "review", verdict="pass" if verdict["pass"] else "fail")

    summary = {
        "project_id": project_id,
        "status": "complete" if verdict["pass"] else "complete_with_issues",
        "verdict": verdict,
        "final_video": str(final_mp4),
        "channel": channel,
        "motion": motion,
        "seconds": round(time.time() - t0, 1),
        "visual": (verdict.get("visual") or {}).get("metrics"),
    }
    summary_path = _projects_dir() / project_id / "harness-summary.json"
    summary_path.write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1), flush=True)
    return summary


def resume_project(project_id: str, **overrides: Any) -> dict[str, Any]:
    """Continue a checkpointed project (skips completed stages)."""
    state = _load_state(project_id)
    if not state.get("completed"):
        raise HarnessError(
            f"project '{project_id}' has no harness state — did a run start?"
        )
    work = _projects_dir() / project_id / "input"
    if "input" in state["completed"] and (work / "script.json").exists():
        # Approval gate resolution: a frozen plan waiting for edits.
        gate = _projects_dir() / project_id / "plan-awaiting-approval.json"
        if gate.exists():
            edited = json.loads(gate.read_text())
            (work / "script.json").write_text(json.dumps(edited, indent=1, ensure_ascii=False))
            gate.unlink()
            state["completed"] = [s for s in state["completed"] if s != "narration"]
            print("[harness] approved plan applied (edited plan detected)", flush=True)
        input_path = work
    else:
        raise HarnessError(f"project '{project_id}' has no input stage to resume from")

    # Only explicit (non-None) overrides replace the checkpointed state —
    # argparse defaults arrive as None and must not shadow saved values.
    opts = {k: v for k, v in overrides.items() if v is not None}
    return run_project(
        input_path,
        project_id=project_id,
        channel=opts.get("channel", state.get("channel", "default")),
        out_dir=opts.get("out_dir"),
        motion=opts.get("motion", state.get("motion", "auto")),
        approval=opts.get("approval", "auto"),
        concurrency=opts.get("concurrency"),
        broll_source=opts.get("broll_source", "auto"),
        no_llm=opts.get("no_llm", False),
        single_gpu=opts.get("single_gpu", False),
        fast_visual=bool(opts.get("fast_visual", False)),
        skip_visual=bool(opts.get("skip_visual", False)),
        vision_review=bool(opts.get("vision_review", True)),
    )


def project_status(project_id: str) -> dict[str, Any]:
    state = _load_state(project_id)
    summary_path = _projects_dir() / project_id / "harness-summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else None
    return {"state": state, "summary": summary}
