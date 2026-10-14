"""Offline SFX + BGM synthesis for the voiceover video pipeline.

Produces, with zero API keys and zero network:
  * whoosh.wav   — band-swept noise (scene transitions, pairs with the luma sweep)
  * impact.wav   — sub thump + noise burst (hero title entrances)
  * riser.wav    — rising tension cue (section changes)
  * chime.wav    — FM bell (progress-bar completions, outro)
  * pop.wav      — soft UI pop (stat card landings)
  * bgm.mp3      — ~150s ambient music bed (pads + sub + pluck arp)

Everything is deterministic (fixed seeds) so renders are reproducible.

Usage:
    python synth_audio.py --out-dir <dir> [--bgm-seconds 150] [--force]

Files already present are reused unless --force.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy import signal as sps

SR = 44100


def _stereoize(mono: np.ndarray, width: float = 0.15, seed: int = 7) -> np.ndarray:
    """Give a mono signal subtle stereo width via a decorrelated copy."""
    rng = np.random.default_rng(seed)
    delay = int(SR * 0.011)
    delayed = np.concatenate([np.zeros(delay), mono[:-delay]])
    # decorrelate slightly with filtered noise modulation
    mod = 1.0 + width * np.convolve(
        rng.standard_normal(len(mono)), np.ones(64) / 64, mode="same"
    )
    left = mono + width * delayed * mod
    right = mono - width * delayed
    return np.stack([left, right], axis=1)


def _norm_stereo(st: np.ndarray, peak: float = 0.85) -> np.ndarray:
    m = np.max(np.abs(st))
    if m > 0:
        st = st * (peak / m)
    return st


def _write_wav(path: Path, st: np.ndarray, peak: float = 0.85) -> None:
    st = _norm_stereo(st, peak)
    wavfile.write(path, SR, (st * 32767).astype(np.int16))


def _env(n: int, attack: float, release: float, curve: float = 2.0) -> np.ndarray:
    """Attack/sustain/release envelope (sustain at 1)."""
    t = np.linspace(0, 1, n)
    a = int(attack * n)
    r = int(release * n)
    env = np.ones(n)
    if a > 0:
        env[:a] = np.linspace(0, 1, a) ** 0.7
    if r > 0:
        env[-r:] = np.minimum(env[-r:], np.linspace(1, 0, r) ** curve)
    return env


# ---------------------------------------------------------------------------
# SFX
# ---------------------------------------------------------------------------

def synth_whoosh(seed: int = 1, dur: float = 0.5) -> np.ndarray:
    """Noise burst with a resonant band sweeping up then down."""
    rng = np.random.default_rng(seed)
    n = int(SR * dur)
    t = np.arange(n) / SR
    noise = rng.standard_normal(n)
    # Time-varying resonator: two cascaded one-poles whose coefficient falls
    # (brightens) through the middle then rises again.
    out = np.empty(n)
    y1 = y2 = 0.0
    for i in range(n):
        p = i / n
        bright = np.sin(np.pi * min(1.0, p * 1.15)) ** 1.2  # 0→1→0
        a = 0.93 - 0.55 * bright
        y1 = a * y1 + (1 - a) * noise[i]
        y2 = a * y2 + (1 - a) * y1
        out[i] = y2
    # Band emphasis around the sweep for "air"
    b, a = sps.butter(2, [400 / (SR / 2), 6500 / (SR / 2)], btype="band")
    out = sps.lfilter(b, a, out)
    env = _env(n, 0.25, 0.45, curve=1.6) ** 1.4
    return out * env * 2.2


def synth_impact(seed: int = 2, dur: float = 0.7) -> np.ndarray:
    """Cinematic thump: pitch-dropping sine + noise transient."""
    rng = np.random.default_rng(seed)
    n = int(SR * dur)
    t = np.arange(n) / SR
    f = 95 * np.exp(-t * 2.2) + 38
    phase = 2 * np.pi * np.cumsum(f) / SR
    body = np.sin(phase) * np.exp(-t * 3.4)
    click = rng.standard_normal(n) * np.exp(-t * 34) * 0.5
    rumble = rng.standard_normal(n)
    b, a = sps.butter(2, 180 / (SR / 2), btype="low")
    rumble = sps.lfilter(b, a, rumble) * np.exp(-t * 2.0) * 0.7
    return body + click + rumble


def synth_riser(seed: int = 3, dur: float = 1.5) -> np.ndarray:
    """Rising noise + sine swell into a cut."""
    rng = np.random.default_rng(seed)
    n = int(SR * dur)
    t = np.arange(n) / SR
    p = t / dur
    noise = rng.standard_normal(n)
    # Time-varying bandpass via 8 crossfaded chunks (robust, no per-sample loop).
    out = np.zeros(n)
    chunks = 8
    overlap = max(1, n // 64)
    for c in range(chunks):
        lo_n = (300 + 500 * (c / chunks)) / (SR / 2)
        hi_n = min((700 + 3800 * ((c + 1) / chunks) ** 1.4) / (SR / 2), 0.46)
        b, a = sps.butter(2, [min(lo_n, hi_n * 0.8), hi_n], btype="band")
        filt = sps.lfilter(b, a, noise)
        start = int(c * (n - overlap) / chunks)
        end = int(min(n, start + (n - overlap) / chunks + overlap))
        w = np.zeros(n)
        seg = end - start
        fade = np.ones(seg)
        fo = min(overlap, seg // 2)
        if fo > 0:
            fade[:fo] = np.linspace(0, 1, fo)
            fade[-fo:] = np.minimum(fade[-fo:], np.linspace(1, 0, fo))
        w[start:end] = fade
        out += filt * w
    f = 180 * (2 ** (p * 2.0))
    tone = np.sin(2 * np.pi * np.cumsum(f) / SR) * 0.25
    env = p**2.2
    return (out * 0.9 + tone) * env


def synth_chime(seed: int = 4, dur: float = 2.0) -> np.ndarray:
    """FM bell: carrier + fifth, exponential decay."""
    n = int(SR * dur)
    t = np.arange(n) / SR
    def bell(f0: float, amp: float, ratio: float) -> np.ndarray:
        mod = np.sin(2 * np.pi * f0 * ratio * t) * np.exp(-t * 2.6)
        return np.sin(2 * np.pi * f0 * t + 2.2 * mod) * np.exp(-t * 2.2) * amp
    return bell(880, 1.0, 2.76) + bell(1318.5, 0.45, 3.01) + bell(587.3, 0.25, 2.0)


def synth_pop(seed: int = 5, dur: float = 0.16) -> np.ndarray:
    """Soft UI pop for stat landings."""
    n = int(SR * dur)
    t = np.arange(n) / SR
    f = 320 * np.exp(-t * 9) + 190
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 20)


# ---------------------------------------------------------------------------
# BGM — ambient bed: Cmin9 → Abmaj7 → Ebmaj9 → Bbsus, pads + sub + pluck arp
# ---------------------------------------------------------------------------

CHORDS = [
    # (root Hz, chord semitone offsets from root)
    (65.41, [0, 3, 7, 10, 14]),   # C2 minor 9
    (51.91, [0, 4, 7, 11, 14]),   # Ab1 maj 7 (add 9)
    (77.78, [0, 4, 7, 10, 14]),   # Eb2 maj 9-ish
    (58.27, [0, 5, 7, 10, 12]),   # Bb1 sus (resolve flavor)
]
BARS_PER_CHORD = 4
BPM = 84


def _sawish(f0: float, n: int, detune_cents: float, seed: int) -> np.ndarray:
    """Band-limited saw-ish tone via additive harmonics (avoids aliasing)."""
    t = np.arange(n) / SR
    f = f0 * 2 ** (detune_cents / 1200)
    out = np.zeros(n)
    rng = np.random.default_rng(seed)
    for h in range(1, 13):
        if f * h > SR / 2 * 0.9:
            break
        # slight per-harmonic wobble for warmth
        wob = 1 + 0.0015 * np.sin(2 * np.pi * (0.3 + 0.17 * h) * t + rng.uniform(0, 6.28))
        out += (1.0 / h) * np.sin(2 * np.pi * f * h * t * wob)
    return out / 2.2


def _pluck(f0: float, n: int, seed: int) -> np.ndarray:
    t = np.arange(n) / SR
    out = np.sin(2 * np.pi * f0 * t) + 0.35 * np.sin(2 * np.pi * 2 * f0 * t) + 0.12 * np.sin(2 * np.pi * 3 * f0 * t)
    return out * np.exp(-t * 6.5)


def synth_bgm(seconds: float = 150.0, seed: int = 42) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n_total = int(SR * seconds)
    left = np.zeros(n_total)
    right = np.zeros(n_total)

    beat = 60.0 / BPM
    bar = 4 * beat
    chord_dur = BARS_PER_CHORD * bar
    n_chord = int(chord_dur * SR)

    # --- Pads + sub, one chord at a time -------------------------------
    pos = 0
    ci = 0
    while pos < n_total:
        root, ivals = CHORDS[ci % len(CHORDS)]
        n = min(n_chord, n_total - pos)
        env = _env(n, 0.35, 0.3, curve=1.5)  # slow swell in/out
        pad_l = np.zeros(n)
        pad_r = np.zeros(n)
        for k, iv in enumerate(ivals):
            f = root * 2 ** (iv / 12) * 2  # pads an octave up
            v_l = _sawish(f, n, -3.0, seed + k * 13)
            v_r = _sawish(f, n, +3.0, seed + k * 13 + 1)
            amp = 0.16 / (1 + k * 0.35)
            pad_l += v_l * amp
            pad_r += v_r * amp
        # Slow lowpass LFO for breathing
        lfo = 0.5 + 0.5 * np.sin(2 * np.pi * 0.07 * np.arange(n) / SR + ci)
        cutoff = 700 + 1900 * lfo
        # apply per-chord average filter (cheap: two fixed filters blended)
        b1, a1 = sps.butter(2, 700 / (SR / 2), btype="low")
        b2, a2 = sps.butter(2, 2400 / (SR / 2), btype="low")
        fl = sps.lfilter(b1, a1, pad_l)
        fr = sps.lfilter(b1, a1, pad_r)
        gl = sps.lfilter(b2, a2, pad_l)
        gr = sps.lfilter(b2, a2, pad_r)
        pad_l = fl * (1 - lfo) + gl * lfo
        pad_r = fr * (1 - lfo) + gr * lfo
        # Sub bass: root sine with per-chord swell
        t = np.arange(n) / SR
        sub = np.sin(2 * np.pi * root * t) * (0.22 * _env(n, 0.25, 0.25))
        left[pos:pos + n] += (pad_l * env + sub * 0.55)
        right[pos:pos + n] += (pad_r * env + sub * 0.55)
        pos += n
        ci += 1

    # --- Pluck arpeggio, 8th notes, quiet, panned ----------------------
    eighth = beat / 2
    n_e = int(eighth * SR)
    step = 0
    pos = int(2 * bar * SR)  # start after 2 bars
    while pos + n_e < n_total:
        root, ivals = CHORDS[int((pos / SR) // chord_dur) % len(CHORDS)]
        iv = ivals[step % len(ivals)]
        oct_up = 2 if (step // len(ivals)) % 2 == 0 else 4
        f = root * 2 ** (iv / 12) * oct_up
        pl = _pluck(f, n_e, seed + step)
        pan = 0.35 + 0.3 * np.sin(step * 0.7)
        g = 0.05 * (0.7 + 0.3 * np.sin(step * 1.3))  # humanized dynamics
        left[pos:pos + n_e] += pl * g * (1 - pan)
        right[pos:pos + n_e] += pl * g * pan
        pos += n_e
        step += 1

    # --- Cheap ambience: soft noise air --------------------------------
    air = rng.standard_normal(n_total)
    b, a = sps.butter(2, [900 / (SR / 2), 2600 / (SR / 2)], btype="band")
    air = sps.lfilter(b, a, air) * 0.012
    left += air
    right += np.roll(air, 511)

    # --- Cheap reverb: two delayed copies ------------------------------
    for delay_s, gain in ((0.083, 0.26), (0.161, 0.13)):
        d = int(SR * delay_s)
        left[d:] += gain * left[:-d]
        right[d:] += gain * right[:-d]

    st = np.stack([left, right], axis=1)
    return _norm_stereo(st, peak=0.5)  # headroom; final level set in the mix


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

SFX_SPECS = [
    ("whoosh.wav", synth_whoosh, 0.8),
    ("impact.wav", synth_impact, 0.9),
    ("riser.wav", synth_riser, 0.7),
    ("chime.wav", synth_chime, 0.6),
    ("pop.wav", synth_pop, 0.7),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--bgm-seconds", type=float, default=150.0)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    for name, fn, peak in SFX_SPECS:
        p = out / name
        if p.exists() and not args.force and p.stat().st_size > 4_000:
            print(f"reuse {name}")
            continue
        mono = fn()
        st = _stereoize(np.asarray(mono, dtype=np.float64))
        _write_wav(p, st, peak=peak)
        print(f"wrote {name} ({p.stat().st_size / 1024:.0f} KB)")

    bgm_wav = out / "bgm.wav"
    bgm_mp3 = out / "bgm.mp3"
    if bgm_mp3.exists() and not args.force and bgm_mp3.stat().st_size > 50_000:
        print("reuse bgm.mp3")
    else:
        import subprocess
        st = synth_bgm(args.bgm_seconds)
        _write_wav(bgm_wav, st, peak=0.5)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(bgm_wav),
             "-codec:a", "libmp3lame", "-b:a", "192k", str(bgm_mp3)], check=True)
        bgm_wav.unlink()
        print(f"wrote bgm.mp3 ({args.bgm_seconds:.0f}s, {bgm_mp3.stat().st_size / 2**20:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
