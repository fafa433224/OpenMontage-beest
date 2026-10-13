"""End-to-end: voiceover (input) -> GPU transcription -> props -> Remotion render -> QC.

The Kaggle production path for the OpenMontage Explainer. Proven on a
T4x2 instance: 712s 1080p30 video, GPU (CUDA) transcription, Remotion
render. This version adds:

  * ``--concurrency`` passthrough to Remotion (4 tabs renders ~1.5x faster
    on the 4-vCPU Kaggle box than the default 2)
  * progress-bar frame QC — extracts frames at each progress_bar cut's
    midpoint so a reviewer (or a VLM) can verify the bar and the counter
    agree with the label. This is the exact check that caught the
    fraction-vs-percent bug fixed in ProgressBar.tsx.

Usage:
    python render_voiceover_video.py <name> <input_dir> <repo_dir> <out_dir> \
        [--concurrency 4]

input_dir must contain: script.json, voiceover.mp3, sections/*.mp3
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from build_explainer_props import build_props  # noqa: E402


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
    args = ap.parse_args()

    input_dir, repo_dir, out_dir = (Path(p) for p in (args.input_dir, args.repo_dir, args.out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    composer = repo_dir / "remotion-composer"
    timing = {"video": args.name, "stages": {}}
    T0 = time.time()

    script = json.loads((input_dir / "script.json").read_text())
    secs = [input_dir / "sections" / f"{s['id']}.mp3" for s in script["sections"]]

    if args.reuse_props:
        with stage("0_reuse_props"):
            props = json.loads(Path(args.reuse_props).read_text())
            durations = [duration(p) for p in secs]  # still needed for QC math
    else:
        # 1. Measure per-section voiceover durations (the timing source of truth)
        with stage("1_measure_voiceover"):
            durations = [duration(p) for p in secs]
            print("section durations", [round(d, 1) for d in durations], "total", round(sum(durations), 1))

        # 2. GPU transcription with the repo's own Transcriber (faster-whisper, CUDA when present)
        with stage("2_transcribe_gpu"):
            sys.path.insert(0, str(repo_dir))
            from tools.analysis.transcriber import Transcriber
            tr = Transcriber().execute({"input_path": str(input_dir / "voiceover.mp3"),
                                        "model_size": "medium", "language": "en",
                                        "output_dir": str(out_dir / "transcript")})
            if not tr.success:
                raise SystemExit(f"transcription failed: {tr.error}")
            words = tr.data["word_timestamps"]
            print("device", tr.data.get("device"), "compute", tr.data.get("compute_type"),
                  "words", len(words), "last word end", words[-1]["end"])
            timing["transcription_device"] = tr.data.get("device")

        # 3. Build props from script + timing + word captions
        with stage("3_build_props"):
            props = build_props(script, durations, words)
            print("cuts", len(props["cuts"]), "captions", len(props["captions"]))

    props_path = out_dir / f"{args.name}-props.json"
    props_path.write_text(json.dumps(props, indent=1, ensure_ascii=False))

    # 4. Remotion render
    with stage("4_npm_install"):
        if not (composer / "node_modules").exists():
            sh("npm ci --no-audit --no-fund", cwd=composer)
        (composer / "public").mkdir(exist_ok=True)
        shutil.copy(input_dir / "voiceover.mp3", composer / "public" / "voiceover.mp3")

    final = out_dir / f"{args.name}.mp4"
    with stage("5_remotion_render"):
        conc = f" --concurrency={args.concurrency}" if args.concurrency else ""
        sh(f"npx remotion render src/index.tsx Explainer '{final}' --props '{props_path}' "
           f"--codec h264 --timeout 120000{conc} --log info", cwd=composer)

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
        if not args.reuse_props:
            script_words = len(" ".join(s["text"] for s in script["sections"]).split())
            qc["script_words"] = script_words
            qc["transcribed_words"] = len(words)
            qc["word_coverage_pct"] = round(100 * len(words) / script_words, 1)
        (out_dir / f"{args.name}-qc.json").write_text(json.dumps(qc, indent=1))
        print(json.dumps(qc, indent=1))
        frames = out_dir / "frames"
        frames.mkdir(exist_ok=True)
        # General spread frames
        for t in [5, vdur * 0.25, vdur * 0.5, vdur * 0.75, vdur - 5]:
            sh(f"ffmpeg -loglevel error -y -ss {t:.2f} -i '{final}' -frames:v 1 -vf scale=960:-1 "
               f"'{frames}/t{int(t)}.png'")
        # Progress-bar cuts: frame at each cut's midpoint, where the fill has
        # settled, so bar+counter can be compared against the label text.
        pb_frames = []
        for c in props["cuts"]:
            if c.get("type") == "progress_bar":
                mid = (c["in_seconds"] + c["out_seconds"]) / 2
                fp = frames / f"progress-{c['id']}.png"
                sh(f"ffmpeg -loglevel error -y -ss {mid:.2f} -i '{final}' -frames:v 1 "
                   f"-vf scale=960:-1 '{fp}'")
                pb_frames.append({"id": c["id"], "label": c.get("progressLabel"),
                                  "progress": c.get("progress"), "frame": str(fp)})
        (out_dir / f"{args.name}-progress-frames.json").write_text(json.dumps(pb_frames, indent=1))
        print(f"progress-bar frames: {len(pb_frames)}")

    timing["total_wall_s"] = round(time.time() - T0, 1)
    (out_dir / f"{args.name}-timing.json").write_text(json.dumps(timing, indent=1))
    print("\nTIMING", json.dumps(timing, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
