"""End-to-end: voiceover (input) -> GPU transcription -> props -> Remotion
body render + HyperFrames intro/outro -> ONE final video -> QC.

The Kaggle production path for the OpenMontage Explainer. Proven on a
T4x2 instance. v3 adds the full creative + dual-GPU pipeline:

  * procedural asset synthesis (SFX + ambient BGM via synth_audio.py,
    stock-footage b-roll clips via synth_broll.py) — zero API keys,
  * v3 creative direction via build_explainer_props.py + the seeded
    creative director: caption STYLE PACKS, per-project palettes, seeded
    transitions, LLM creative brief when a runtime LLM is configured
    (LLM_BASE_URL/LLM_API_KEY/LLM_MODEL or any provider key),
  * Open-Sora-Dataset b-roll: when OPEN_SORA_DIR points at a downloaded
    dataset with a LanceDB+CLIP index, real footage replaces the synth
    pool (queries derived from the script + LLM mood words), with true
    per-clip durations so the renderer loops them gaplessly (lag fix),
  * dual-GPU: on T4x2, whisper transcription runs on GPU 0 while the
    Open-Sora CLIP search/indexing subprocess runs on GPU 1 — both
    accelerators busy, wall time reduced,
  * HyperFrames-rendered kinetic intro + outro (HTML + GSAP) concatenated
    with the Remotion-rendered body — BOTH engines making one video,
  * progress-bar frame QC and full-video census frames for review.

Usage:
    python render_voiceover_video.py <name> <input_dir> <repo_dir> <out_dir> \
        [--concurrency 4] [--skip-hyperframes] [--skip-asset-synth] \
        [--broll-source auto|synth|open-sora] [--no-llm] [--single-gpu]

input_dir must contain: script.json, voiceover.mp3, sections/*.mp3

Requires Node >= 22 on PATH for the HyperFrames stage (on Kaggle, install
the Node 22 tarball first — see kernels/render-v5). With --skip-hyperframes
the final video is the Remotion body alone (loud warning printed).
"""
import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from build_explainer_props import build_props  # noqa: E402

HYPERFRAMES_TEMPLATES = HERE / "hyperframes"


def stage(label):
    class _S:
        def __enter__(self_):
            self_.t = time.time()
            print(f"\n=== [{label}] start", flush=True)
            return self_

        def __exit__(self_, *exc):
            dt = time.time() - self_.t
            print(f"=== [{label}] done in {dt:.1f}s", flush=True)
            if exc[0] is not None:
                raise exc[1]
    return _S()


def sh(cmd, cwd=None):
    print("$", cmd, flush=True)
    subprocess.run(cmd, shell=True, cwd=cwd, check=True)


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True).stdout
    return float(out.strip())


def node_major() -> int:
    try:
        out = subprocess.run(["node", "--version"], capture_output=True, text=True).stdout.strip()
        return int(out.lstrip("v").split(".")[0])
    except Exception:
        return 0


def detect_gpus() -> list:
    """List CUDA device indices via nvidia-smi (empty on CPU-only boxes)."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return [line.split(",")[0].strip() for line in out.splitlines() if line.strip()]
    except Exception:
        return []


def llm_brief_for(script):
    """Ask the runtime LLM for a creative brief; None when unconfigured.

    The brief only SHIFTS creative choices (mood, palette family, caption
    style, pacing, b-roll mood words) — the seeded director guarantees a
    valid, varied plan with or without it. Never fatal."""
    try:
        repo_root = HERE.parents[1]
        if repo_root not in [Path(p) for p in sys.path]:
            sys.path.insert(0, str(repo_root))
        from lib.llm_client import LLMClient, LLMError
        from lib.creative_director import llm_creative_brief

        client = LLMClient()
        if not client.available:
            print("LLM: not configured — seeded creative director active "
                  "(set LLM_BASE_URL or a provider key for LLM direction)", flush=True)
            return None
        print(f"LLM: {client.provider} / {client.model} — requesting creative brief", flush=True)
        brief = llm_creative_brief(script, client)
        if brief:
            print("LLM brief:", json.dumps(brief, ensure_ascii=False)[:400], flush=True)
        return brief
    except Exception as exc:
        print(f"LLM brief unavailable ({exc}) — seeded director active", flush=True)
        return None


def collect_broll_parallel(script, brief, composer, out_dir, gpu_id, timeout_s=900):
    """Spawn the Open-Sora collector on a specific GPU; returns (proc, res_path).

    Runs concurrently with whisper transcription so both T4s stay busy.
    The caller must eventually ``communicate(timeout=timeout_s)`` the
    returned process (see ``2c_harvest_broll``) — when Open-Sora is not set
    up the collector writes an empty result and the caller falls back to
    the procedural synth pool."""
    stage_dir = composer / "public" / "broll"
    queries = []
    moods = [str(m) for m in (brief or {}).get("broll_moods", [])][:3]
    for m in moods:
        queries.append({"q": m, "n": 3})
    for s in script.get("sections", [])[:6]:
        queries.append({"q": f"{s.get('heading', '')} {moods[0] if moods else ''}".strip(), "n": 2})
    if not queries:
        queries = [{"q": "abstract cinematic background", "n": 6}]

    job = {
        "queries": queries,
        "stage_dir": str(stage_dir),
        "max_clips": 14,
        "dataset_dir": os.environ.get("OPEN_SORA_DIR", ""),
    }
    job_path = out_dir / "broll-job.json"
    res_path = out_dir / "broll-result.json"
    job_path.write_text(json.dumps(job, ensure_ascii=False))

    env = dict(os.environ)
    if gpu_id is not None:
        env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)  # CLIP embedder runs here
    cmd = [sys.executable, str(HERE / "collect_broll_open_sora.py"),
           "--job", str(job_path), "--out", str(res_path)]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    return proc, res_path


def synth_assets(composer: Path, skip: bool, skip_broll: bool = False) -> None:
    """Generate SFX/BGM + b-roll into remotion-composer/public/.

    ``skip_broll`` is set when real Open-Sora footage was already staged —
    the synth clips are only the zero-key fallback.
    """
    pub = composer / "public"
    (pub / "audio").mkdir(parents=True, exist_ok=True)
    (pub / "broll").mkdir(parents=True, exist_ok=True)
    if skip:
        print("WARNING: --skip-asset-synth — reusing whatever audio/broll exists", flush=True)
        return
    # SFX + BGM (fast; reuses existing files)
    sh(f"'{sys.executable}' '{HERE / 'synth_audio.py'}' --out-dir '{pub / 'audio'}' --bgm-seconds 160")
    # B-roll (slower; reuses existing files) — skipped when real footage is staged
    if skip_broll:
        print("real Open-Sora footage staged — skipping synth b-roll generation", flush=True)
        return
    sh(f"'{sys.executable}' '{HERE / 'synth_broll.py'}' --out-dir '{pub / 'broll'}' --seconds 8")


def render_hyperframes(work: Path, template: str, sfx_names: list, out_mp4: Path,
                       gsap_src: Path, audio_src: Path) -> None:
    """Materialize a HyperFrames workspace from a repo template and render it.

    The CLI can hang in teardown AFTER writing a complete output file
    (observed locally: file complete at ~40s, process still alive at 9min).
    So we run it under `timeout`, then judge success by probing the OUTPUT
    FILE's duration — the only ground truth that matters.
    """
    ws = work / f"hf-{template}"
    (ws / "assets").mkdir(parents=True, exist_ok=True)
    shutil.copy(HYPERFRAMES_TEMPLATES / template / "index.html", ws / "index.html")
    shutil.copy(gsap_src, ws / "assets" / "gsap.min.js")
    for name in sfx_names:
        shutil.copy(audio_src / name, ws / "assets" / name)
    expected = {"intro": 5.4, "outro": 4.8}[template]
    cmd = (f"cd '{ws}' && timeout 420 npx --yes hyperframes render . -o '{out_mp4}' "
           f"--fps 30 --quality standard --browser-timeout 120 > render.log 2>&1; "
           f"echo hyperframes_exit=$?")
    subprocess.run(cmd, shell=True, cwd=work)  # rc handled by the file probe below
    log_tail = (ws / "render.log").read_text(errors="replace")[-1500:] if (ws / "render.log").exists() else ""
    if not out_mp4.exists():
        raise SystemExit(f"hyperframes render produced no output for {template}:\n{log_tail}")
    d = duration(out_mp4)
    if abs(d - expected) > 0.35:
        raise SystemExit(f"hyperframes render for {template} has bad duration {d}s "
                         f"(expected {expected}s):\n{log_tail}")
    print(f"hyperframes {template}: {d:.2f}s OK (log tail: {log_tail[-300:]})", flush=True)


def concat_videos(parts: list, out: Path) -> None:
    """Re-encoding concat (uniform h264/aac) — robust against timebase and
    codec-parameter mismatches between Remotion and HyperFrames outputs."""
    inputs = " ".join(f"-i '{p}'" for p in parts)
    n = len(parts)
    fc = f"[0:v][0:a]" + "".join(f"[{i}:v][{i}:a]" for i in range(1, n)) + \
         f"concat=n={n}:v=1:a=1[v][a]"
    sh(f"ffmpeg -y -loglevel error {inputs} -filter_complex '{fc}' -map '[v]' -map '[a]' "
       f"-c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p -r 30 "
       f"-c:a aac -b:a 192k -ar 48000 -movflags +faststart '{out}'")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("input_dir")
    ap.add_argument("repo_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--concurrency", type=int, default=None,
                    help="Remotion render tabs (default: Remotion's own default)")
    ap.add_argument("--reuse-props", default=None,
                    help="path to an existing props json; skips measure/transcribe/build")
    ap.add_argument("--skip-hyperframes", action="store_true",
                    help="render Remotion body only (no intro/outro)")
    ap.add_argument("--skip-asset-synth", action="store_true",
                    help="reuse existing audio/broll assets without regenerating")
    ap.add_argument("--broll-source", choices=["auto", "synth", "open-sora"], default="auto",
                    help="b-roll footage source: auto = Open-Sora when indexed, else synth")
    ap.add_argument("--no-llm", action="store_true",
                    help="skip the runtime LLM creative brief (seeded director only)")
    ap.add_argument("--single-gpu", action="store_true",
                    help="force everything onto GPU 0 (no parallel CLIP subprocess)")
    args = ap.parse_args()

    input_dir, repo_dir, out_dir = (Path(p) for p in (args.input_dir, args.repo_dir, args.out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    composer = repo_dir / "remotion-composer"
    timing = {"video": args.name, "stages": {}}
    T0 = time.time()

    script = json.loads((input_dir / "script.json").read_text())
    secs = [input_dir / "sections" / f"{s['id']}.mp3" for s in script["sections"]]

    # v3: GPU topology + LLM creative brief (before any heavy stage).
    gpus = detect_gpus()
    gpu_whisper = gpus[0] if gpus else None
    gpu_clip = (gpus[1] if len(gpus) > 1 and not args.single_gpu else
                (gpus[0] if gpus else None))
    timing["gpus"] = gpus
    print(f"GPU topology: {gpus or 'CPU-only'} | whisper -> {gpu_whisper} | CLIP/broll -> {gpu_clip}", flush=True)
    if len(gpus) > 1 and not args.single_gpu:
        print("dual-GPU mode: transcription (GPU 0) runs in parallel with "
              "Open-Sora b-roll collection (GPU 1)", flush=True)

    brief = None
    if not args.no_llm:
        brief = llm_brief_for(script)
        timing["llm_brief"] = bool(brief)

    if args.reuse_props:
        with stage("0_reuse_props"):
            props = json.loads(Path(args.reuse_props).read_text())
            durations = [duration(p) for p in secs]  # still needed for QC math
    else:
        # 1. Measure per-section voiceover durations (the timing source of truth)
        with stage("1_measure_voiceover"):
            durations = [duration(p) for p in secs]
            print("section durations", [round(d, 1) for d in durations], "total", round(sum(durations), 1))

        # 2 + 2b. DUAL-GPU PARALLEL: whisper transcription on GPU 0 while the
        # Open-Sora CLIP collector subprocess owns GPU 1. On single-GPU /
        # CPU boxes the collector still runs (cheap when the index exists) —
        # CLIP shares the GPU with whisper only when actually embedding.
        broll_proc = None
        broll_res_path = None
        want_open_sora = args.broll_source in ("auto", "open-sora")
        if want_open_sora:
            with stage("2b_spawn_broll_collector"):
                broll_proc, broll_res_path = collect_broll_parallel(
                    script, brief, composer, out_dir, gpu_clip)

        with stage("2_transcribe_gpu"):
            try:
                if gpu_whisper is not None:
                    os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_whisper)
                sys.path.insert(0, str(repo_dir))
                from tools.analysis.transcriber import Transcriber
                tr = Transcriber().execute({"input_path": str(input_dir / "voiceover.mp3"),
                                            "model_size": "medium", "language": "en",
                                            "output_dir": str(out_dir / "transcript")})
                if not tr.success:
                    raise SystemExit(f"transcription failed: {tr.error}")
            except BaseException:
                # Never orphan the GPU-1 collector if transcription dies —
                # it would hold the second T4 until the Kaggle session ends.
                if broll_proc is not None and broll_proc.poll() is None:
                    broll_proc.kill()
                raise
            words = tr.data["word_timestamps"]
            print("device", tr.data.get("device"), "compute", tr.data.get("compute_type"),
                  "words", len(words), "last word end", words[-1]["end"])
            timing["transcription_device"] = tr.data.get("device")

        # Harvest the Open-Sora collector (it has been running in parallel).
        broll_entries = []
        if broll_proc is not None:
            with stage("2c_harvest_broll"):
                try:
                    out, _ = broll_proc.communicate(timeout=900)
                    if out:
                        print("[broll-collector]", out[-600:], flush=True)
                    if broll_res_path and broll_res_path.exists():
                        res = json.loads(broll_res_path.read_text())
                        broll_entries = res.get("entries", [])
                        timing["broll_source"] = ("open_sora" if broll_entries else "synth_fallback")
                        timing["broll_note"] = res.get("note", "")
                        print(f"b-roll: {res.get('note', '')} | {len(broll_entries)} real clips", flush=True)
                except subprocess.TimeoutExpired:
                    broll_proc.kill()
                    print("WARNING: b-roll collector timed out — using synth pool", flush=True)
        if args.broll_source == "open-sora" and not broll_entries:
            print("WARNING: --broll-source=open-sora but no clips were staged — "
                  "falling back to the synth pool", flush=True)

        # 3. Build props from script + timing + word captions (+ creative brief
        #    + real b-roll with durations for gapless looping)
        with stage("3_build_props"):
            props = build_props(script, durations, words,
                                seed=script.get("topic"),
                                brief=brief,
                                broll_pool=broll_entries or None)
            print("cuts", len(props["cuts"]), "captions", len(props["captions"]),
                  "sfx", len(props.get("sfx", [])),
                  "caption style", props.get("captionStyle", {}).get("look"),
                  "mood", props.get("creativeBrief", {}).get("mood"))

    props_path = out_dir / f"{args.name}-props.json"
    props_path.write_text(json.dumps(props, indent=1, ensure_ascii=False))

    # 4. npm install + asset synthesis (SFX/BGM/b-roll staged into public/)
    with stage("4_npm_install"):
        if not (composer / "node_modules").exists():
            sh("npm ci --no-audit --no-fund", cwd=composer)
        (composer / "public").mkdir(exist_ok=True)
        shutil.copy(input_dir / "voiceover.mp3", composer / "public" / "voiceover.mp3")

    with stage("4b_synth_assets"):
        # Real Open-Sora footage staged? Skip the synth b-roll generation
        # entirely (SFX + BGM still synthesized — zero-key audio).
        have_real_broll = bool(timing.get("broll_source") == "open_sora")
        synth_assets(composer, args.skip_asset_synth, skip_broll=have_real_broll)

    body = out_dir / f"{args.name}-body.mp4"
    with stage("5_remotion_render"):
        conc = f" --concurrency={args.concurrency}" if args.concurrency else ""
        sh(f"npx remotion render src/index.tsx Explainer '{body}' --props '{props_path}' "
           f"--codec h264 --timeout 120000{conc} --log info", cwd=composer)

    # 5b. HyperFrames intro + outro, then concat into ONE video.
    #     Failures here (missing Chrome libs, runtime quirks) must NOT lose
    #     the rendered body — fall back to a body-only final and QC it.
    final = body
    use_hf = not args.skip_hyperframes
    if use_hf and node_major() < 22:
        print(f"WARNING: Node {node_major()} < 22 — HyperFrames unavailable. "
              "Rendering Remotion body only. Install Node 22 to get the intro/outro.",
              flush=True)
        use_hf = False

    if use_hf:
        try:
            with stage("5c_hyperframes_intro_outro"):
                gsap_src = HYPERFRAMES_TEMPLATES / "assets" / "gsap.min.js"
                audio_src = composer / "public" / "audio"
                intro = out_dir / "intro.mp4"
                outro = out_dir / "outro.mp4"
                render_hyperframes(out_dir, "intro",
                                   ["riser.wav", "impact.wav", "whoosh.wav"], intro,
                                   gsap_src, audio_src)
                render_hyperframes(out_dir, "outro",
                                   ["chime.wav", "whoosh.wav"], outro,
                                   gsap_src, audio_src)
                print("intro", round(duration(intro), 2), "s / outro", round(duration(outro), 2), "s")
            with stage("5d_concat"):
                final = out_dir / f"{args.name}.mp4"
                concat_videos([intro, body, outro], final)
                timing["hyperframes_intro_s"] = round(duration(intro), 2)
                timing["hyperframes_outro_s"] = round(duration(outro), 2)
        except (SystemExit, RuntimeError, OSError) as e:
            print("\n" + "!" * 78, flush=True)
            print(f"WARNING: HyperFrames stage failed — delivering the Remotion body "
                  f"WITHOUT intro/outro. The body itself is complete and correct.\n"
                  f"Reason: {e}\n"
                  f"Kaggle fix: sudo apt-get install -y libnss3 libnspr4 libatk1.0-0 "
                  f"libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 "
                  f"libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 "
                  f"libpango-1.0-0 libcairo2 libasound2", flush=True)
            print("!" * 78 + "\n", flush=True)
            use_hf = False

    if not use_hf:
        final = out_dir / f"{args.name}.mp4"
        if final != body and not final.exists():
            shutil.copy(body, final)

    # 6. QC: real checks, not a smoke test
    with stage("6_qc"):
        vdur = duration(final)
        info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                               "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
                               "-of", "json", str(final)], capture_output=True, text=True).stdout
        astreams = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                                   "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(final)],
                                  capture_output=True, text=True).stdout.strip()
        qc = {"video_duration_s": round(vdur, 2), "expected_duration_s": round(sum(durations), 2),
              "duration_error_s": round(vdur - sum(durations), 2), "stream": json.loads(info)["streams"][0],
              "audio_codec": astreams, "file_mb": round(final.stat().st_size / 2**20, 1)}
        if use_hf:
            qc["intro_outro_included"] = True
            qc["duration_error_s"] = round(vdur - sum(durations) - timing.get("hyperframes_intro_s", 0)
                                           - timing.get("hyperframes_outro_s", 0), 2)
        if not args.reuse_props:
            script_words = len(" ".join(s["text"] for s in script["sections"]).split())
            qc["script_words"] = script_words
            qc["transcribed_words"] = len(words)
            qc["word_coverage_pct"] = round(100 * len(words) / script_words, 1)
        (out_dir / f"{args.name}-qc.json").write_text(json.dumps(qc, indent=1))
        print(json.dumps(qc, indent=1))
        frames = out_dir / "frames"
        frames.mkdir(exist_ok=True)
        # General spread frames (clamped to the actual duration — ffmpeg
        # fails on negative -ss, which would kill an already-successful run)
        for t in [t for t in (5, vdur * 0.25, vdur * 0.5, vdur * 0.75, vdur - 5)
                  if 0 <= t < vdur]:
            sh(f"ffmpeg -loglevel error -y -ss {t:.2f} -i '{final}' -frames:v 1 -vf scale=960:-1 "
               f"'{frames}/t{int(t)}.png'")
        # Census frames: one at EVERY cut midpoint (charts at 70%), where the
        # scene is fully settled — this is what a VLM reviews for visual QC.
        census = frames / "census"
        census.mkdir(exist_ok=True)
        offsets = []
        for c in props["cuts"]:
            frac = 0.7 if c.get("type") in ("bar_chart", "line_chart", "pie_chart", "kpi_grid") else 0.5
            mid = c["in_seconds"] + (c["out_seconds"] - c["in_seconds"]) * frac
            if use_hf:
                mid += timing.get("hyperframes_intro_s", 0)  # census maps onto the FINAL video
            offsets.append((c["id"], c.get("type"), mid))
        meta = []
        for cid, ctype, t in offsets:
            fp = census / f"{cid}.{ctype}.png"
            sh(f"ffmpeg -loglevel error -y -ss {t:.2f} -i '{final}' -frames:v 1 "
               f"-vf scale=960:-1 '{fp}'")
            meta.append({"id": cid, "type": ctype, "t": round(t, 2), "frame": str(fp)})
        (out_dir / f"{args.name}-census.json").write_text(json.dumps(meta, indent=1))
        print(f"census frames: {len(meta)}")

    timing["total_wall_s"] = round(time.time() - T0, 1)
    (out_dir / f"{args.name}-timing.json").write_text(json.dumps(timing, indent=1))
    print("\nTIMING", json.dumps(timing, indent=1))
    print(f"\nFINAL VIDEO: {final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
