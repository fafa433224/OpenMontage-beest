"""Procedural stock-footage (b-roll) generator for the voiceover pipeline.

Produces six 8-second 1920x1080@30 cinematic abstract clips with zero API
keys and zero network — the "stock footage" layer for hero/title scenes:

  * mesh-flow    — animated metaball gradient field (premium tech look)
  * bokeh        — floating soft-focus light discs with parallax
  * light-leak   — warm anamorphic light sweep across a dark frame
  * particles    — drifting dust motes with depth
  * wave-ribbons — flowing luminous sine ribbons
  * aurora       — vertical aurora curtains

Frames are computed at 640x360 (numpy) and upscaled to 1920x1080 by ffmpeg's
lanczos scaler: for soft background footage this is visually equivalent to
native rendering and ~9x cheaper. Encode follows the repo's stock-clip
contract from the HyperFrames gotchas: H.264, yuv420p, -g 30 -keyint_min 30
-sc_threshold 0 -movflags +faststart (dense keyframes so staged playback
never stalls).

Two color worlds are generated per style (cool / warm) and the palette
scrim in Explainer tints them toward the section palette.

Usage:
    python synth_broll.py --out-dir <dir> [--seconds 8] [--styles all] [--force]
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

W, H = 640, 360
FPS = 30

COOL = {
    "deep": (8, 12, 30),
    "a": (94, 84, 230),
    "b": (40, 190, 200),
    "hi": (160, 200, 255),
}
WARM = {
    "deep": (26, 14, 8),
    "a": (235, 140, 52),
    "b": (210, 60, 70),
    "hi": (255, 220, 170),
}


def _lerp_rgb(c1, c2, t):
    return tuple(int(round(a + (b - a) * t)) for a, b in zip(c1, c2))


def _radial_field(shape, centers, colors, deep):
    """Render soft radial-gradient metaballs into an RGB array (uint8)."""
    hh, ww = shape
    y, x = np.mgrid[0:hh, 0:ww].astype(np.float32)
    acc = np.zeros((hh, ww, 3), dtype=np.float32)
    for (cx, cy, r, amp), col in zip(centers, colors):
        d2 = ((x - cx) ** 2 + (y - cy) ** 2) / (r * r)
        wgt = amp * np.exp(-d2 * 2.2)
        acc += wgt[..., None] * np.array(col, dtype=np.float32)
    base = np.array(deep, dtype=np.float32)
    out = base[None, None, :] + acc
    return np.clip(out, 0, 255).astype(np.uint8)


def frames_mesh_flow(pal, n, seed=11):
    rng = np.random.default_rng(seed)
    blobs = [
        {
            "cx": rng.uniform(80, W - 80), "cy": rng.uniform(60, H - 60),
            "r": rng.uniform(110, 210), "amp": rng.uniform(0.55, 1.0),
            "vx": rng.uniform(-14, 14), "vy": rng.uniform(-9, 9),
            "ph": rng.uniform(0, 6.28),
        }
        for _ in range(6)
    ]
    colors = [pal["a"], pal["b"], pal["a"], pal["hi"], pal["b"], pal["a"]]
    for f in range(n):
        t = f / FPS
        centers = []
        for b in blobs:
            cx = b["cx"] + b["vx"] * t + 22 * np.sin(t * 0.5 + b["ph"])
            cy = b["cy"] + b["vy"] * t + 14 * np.cos(t * 0.4 + b["ph"])
            # wrap around so motion never ends
            cx = (cx + W) % (W + 300) - 150
            cy = (cy + H) % (H + 220) - 110
            centers.append((cx, cy, b["r"], b["amp"]))
        yield _radial_field((H, W), centers, colors, pal["deep"])


def frames_bokeh(pal, n, seed=22):
    rng = np.random.default_rng(seed)
    hh, ww = H, W
    # Discs: soft radial gradients, varied hues between palette stops,
    # depth controls size/softness/brightness (far = bigger, softer, dimmer).
    discs = []
    for _ in range(22):
        depth = rng.uniform(0.15, 1.0)
        mix = rng.uniform(0, 1)
        discs.append({
            "x": rng.uniform(-40, ww + 40), "y": rng.uniform(-40, hh + 40),
            "r": (34 - 8 * depth) * rng.uniform(0.7, 1.3),
            "depth": depth,
            "vx": rng.uniform(-7, 7), "vy": rng.uniform(-11, -2),
            "col": _lerp_rgb(_lerp_rgb(pal["a"], pal["b"], mix), pal["hi"], rng.uniform(0, 0.5)),
            "tw": rng.uniform(0.15, 0.6),
            "ph": rng.uniform(0, 6.28),
        })
    yy, xx = np.mgrid[0:hh, 0:ww].astype(np.float32)
    base = np.array(pal["deep"], dtype=np.float32)
    for f in range(n):
        t = f / FPS
        img = base[None, None, :] + np.zeros((hh, ww, 3), dtype=np.float32)
        for d in discs:
            x = (d["x"] + d["vx"] * t + 10 * np.sin(t * d["tw"] * 4 + d["ph"])) % (ww + 120) - 60
            y = (d["y"] + d["vy"] * t) % (hh + 120) - 60
            dist2 = ((xx - x) ** 2 + (yy - y) ** 2) / (d["r"] ** 2)
            # layered falloff: bright core + soft halo (no hard edge anywhere)
            core = np.exp(-dist2 * 2.0)
            halo = 0.45 * np.exp(-dist2 * 0.55)
            alpha = d["depth"] * (0.55 + 0.45 * np.sin(t * d["tw"] * 6 + d["ph"]))
            wgt = (core + halo) * alpha
            img += wgt[..., None] * (0.28 + 0.5 * d["depth"]) * np.array(d["col"], dtype=np.float32)
        # gentle vignette for lens realism
        vx = (xx / ww - 0.5) ** 2 + (yy / hh - 0.5) ** 2
        img *= (1.0 - 0.35 * np.clip(vx * 2.2, 0, 1))[..., None]
        yield np.clip(img, 0, 255).astype(np.uint8)


def frames_light_leak(pal, n, seed=33):
    rng = np.random.default_rng(seed)
    grain = rng.normal(0, 1, (H, W)).astype(np.float32)
    phase0 = rng.uniform(0, 6.28)
    for f in range(n):
        t = f / FPS
        # sweep position -0.3 → 1.3 over the clip
        p = -0.3 + 1.6 * t / (n / FPS)
        x = np.linspace(0, 1, W, dtype=np.float32)[None, :]
        y = np.linspace(0, 1, H, dtype=np.float32)[:, None]
        # band centered at p with soft falloff + vertical tilt
        band = np.exp(-((x + 0.08 * y - p) ** 2) / 0.018)
        leak = band * (0.85 + 0.15 * np.sin(6 * x * np.pi + phase0))
        img = np.zeros((H, W, 3), dtype=np.float32)
        for k, col in enumerate(pal["a"]):
            img[..., k] = pal["deep"][k] * (1 - 0.55 * leak) + col * leak * 0.75
        # film grain
        img += (grain * 6 * (0.4 + 0.6 * band))[..., None]
        yield np.clip(img, 0, 255).astype(np.uint8)


def frames_particles(pal, n, seed=44):
    rng = np.random.default_rng(seed)
    motes = [
        {
            "x": rng.uniform(0, W), "y": rng.uniform(0, H),
            "r": rng.uniform(1.2, 4.5), "depth": rng.uniform(0.25, 1.0),
            "vx": rng.uniform(-8, 8), "vy": rng.uniform(-16, -4),
            "ph": rng.uniform(0, 6.28),
        }
        for _ in range(60)
    ]
    from PIL import ImageDraw
    # Soft base glow (radial falloff — a hard ellipse edge would read as a
    # raw generated asset once footage is dimmed under a scrim).
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    for f in range(n):
        t = f / FPS
        img = Image.new("RGB", (W, H), pal["deep"])
        # base glow as soft radial gradient
        gx, gy, gr = W * 0.58, H * 0.62, W * 0.42
        d2 = ((xx - gx) ** 2 + (yy - gy) ** 2) / (gr * gr)
        glow = np.exp(-d2 * 2.0)
        base = np.array(pal["deep"], dtype=np.float32)
        tint = np.array(_lerp_rgb(pal["deep"], pal["a"], 0.35), dtype=np.float32)
        img_arr = base[None, None, :] + glow[..., None] * (tint - base)[None, None, :] * 0.8
        img = Image.fromarray(np.clip(img_arr, 0, 255).astype(np.uint8))
        draw = ImageDraw.Draw(img, "RGBA")
        for m in sorted(motes, key=lambda m: m["depth"]):
            x = (m["x"] + m["vx"] * t + 6 * np.sin(t * 0.7 + m["ph"])) % (W + 20) - 10
            y = (m["y"] + m["vy"] * t) % (H + 20) - 10
            alpha = int(200 * m["depth"])
            r = m["r"] * (0.8 + 0.4 * m["depth"])
            col = _lerp_rgb(pal["hi"], pal["a"], 0.5)
            # soft dot: dim halo + bright core
            draw.ellipse([x - r * 2, y - r * 2, x + r * 2, y + r * 2], fill=(*col, int(alpha * 0.25)))
            draw.ellipse([x - r, y - r, x + r, y + r], fill=(*pal["hi"], alpha))
        arr = np.asarray(img).astype(np.float32)
        # slight motion blur feel: blend with previous via vertical smear
        if f > 0:
            arr = 0.82 * arr + 0.18 * np.roll(prev, 2, axis=0)
        prev = arr
        yield np.clip(arr, 0, 255).astype(np.uint8)


def frames_wave_ribbons(pal, n, seed=55):
    from PIL import ImageDraw
    rng = np.random.default_rng(seed)
    ribbons = [
        {
            "base": rng.uniform(H * 0.25, H * 0.8),
            "amp": rng.uniform(18, 46),
            "len": rng.uniform(180, 420),
            "speed": rng.uniform(-0.5, 0.5),
            "thick": rng.uniform(3, 9),
            "col": pal["a"] if rng.random() < 0.5 else pal["b"],
            "glow": rng.uniform(60, 140),
        }
        for _ in range(7)
    ]
    for f in range(n):
        t = f / FPS
        img = Image.new("RGB", (W, H), pal["deep"])
        draw = ImageDraw.Draw(img, "RGBA")
        # background wash
        draw.ellipse([-W * 0.2, H * 0.1, W * 1.2, H * 1.1],
                     fill=(*_lerp_rgb(pal["deep"], pal["b"], 0.12), 255))
        for r in ribbons:
            pts = []
            for x in range(0, W + 8, 8):
                yv = r["base"] + r["amp"] * np.sin(2 * np.pi * x / r["len"] + t * r["speed"] * 2 * np.pi)
                pts.append((x, yv))
            # glow underlay
            draw.line(pts, fill=(*r["col"], int(r["glow"] * 0.35)), width=int(r["thick"] * 4))
            draw.line(pts, fill=(*r["col"], int(r["glow"])), width=int(r["thick"]))
            draw.line(pts, fill=(*pal["hi"], 110), width=max(1, int(r["thick"] * 0.4)))
        yield np.asarray(img)


def frames_aurora(pal, n, seed=66):
    rng = np.random.default_rng(seed)
    curtains = [
        {
            "x0": rng.uniform(0, W),
            "width": rng.uniform(60, 190),
            "drift": rng.uniform(-12, 12),
            "wob": rng.uniform(10, 34),
            "wlen": rng.uniform(120, 320),
            "speed": rng.uniform(0.15, 0.5),
            "col": pal["a"] if rng.random() < 0.5 else pal["b"],
            "amp": rng.uniform(0.5, 1.0),
        }
        for _ in range(5)
    ]
    x = np.linspace(0, W, W, dtype=np.float32)[None, :]
    y = np.linspace(0, H, H, dtype=np.float32)[:, None]
    for f in range(n):
        t = f / FPS
        img = np.zeros((H, W, 3), dtype=np.float32)
        img += np.array(pal["deep"], dtype=np.float32)[None, None, :]
        for c in curtains:
            cx = (c["x0"] + c["drift"] * t) % (W + 200) - 100
            wob = c["wob"] * np.sin(2 * np.pi * y / c["wlen"] + t * c["speed"] * 2 * np.pi)
            dist = (x + wob - cx) / c["width"]
            band = np.exp(-dist ** 2 * 3.0) * c["amp"]
            # vertical fade (brighter top, fade to base at bottom)
            vfade = np.clip(1.15 - y / (H * 0.85), 0, 1) ** 1.3
            band = band * vfade
            img += band[..., None] * np.array(c["col"], dtype=np.float32)
        # subtle star specks
        if f % 3 == 0:
            stars = rng.random((H, W)) < 0.00008
            img[stars] = np.array(pal["hi"], dtype=np.float32)
        yield np.clip(img, 0, 255).astype(np.uint8)


STYLES = {
    "mesh-flow": frames_mesh_flow,
    "bokeh": frames_bokeh,
    "light-leak": frames_light_leak,
    "particles": frames_particles,
    "wave-ribbons": frames_wave_ribbons,
    "aurora": frames_aurora,
}


def encode_clip(frames_iter, out_path: Path, seconds: int, scale_to: str = "1920:1080") -> None:
    """Pipe numpy frames → ffmpeg → H.264 with the repo's stock-clip contract."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
        "-i", "-",
        "-vf", f"scale={scale_to}:flags=lanczos,format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-g", "30", "-keyint_min", "30", "-sc_threshold", "0",
        "-movflags", "+faststart", "-an",
        str(out_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for frame in frames_iter:
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg failed for {out_path}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--seconds", type=int, default=8)
    ap.add_argument("--styles", default="all", help="comma list or 'all'")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    styles = list(STYLES) if args.styles == "all" else [s.strip() for s in args.styles.split(",")]
    n = args.seconds * FPS

    for name in styles:
        for world, pal in (("cool", COOL), ("warm", WARM)):
            final = out / f"{name}-{world}.mp4"
            if final.exists() and not args.force:
                print(f"reuse {final.name}")
                continue
            gen = STYLES[name](pal, n)
            encode_clip(gen, final, args.seconds)
            print(f"wrote {final.name} ({final.stat().st_size / 2**20:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
