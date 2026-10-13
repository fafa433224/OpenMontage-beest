import { AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig } from "remotion";
import { seededRandom, type ScenePalette } from "./palettes";

// ---------------------------------------------------------------------------
// SceneBackground — a per-cut animated background.
//
// v1 had ONE global AnimatedBackground for the whole video; it never changed
// when a new line started. This component is rendered INSIDE each cut's
// Sequence, so frame 0 is the first frame of that cut, and it:
//   * picks one of eight distinct animated styles,
//   * uses the section palette (color world changes per section),
//   * crossfades in over the first 12 frames and out over the last 12, so
//     consecutive backgrounds blend instead of popping.
//
// Performance contract (learned from the fb7cf87 orb fix):
//   * NO `filter: blur()` anywhere — glows are radial-gradients.
//   * Animate only transform / opacity / background-position-style math.
//   * Bounded element counts (≤ 5 blobs, ≤ 48 particles).
// ---------------------------------------------------------------------------

export const BG_STYLES = [
  "aurora",
  "mesh",
  "beams",
  "particles",
  "grid-pulse",
  "orbits",
  "waves",
  "grain",
] as const;

export type BgStyle = (typeof BG_STYLES)[number];

export function bgStyleForIndex(i: number): BgStyle {
  return BG_STYLES[((i % BG_STYLES.length) + BG_STYLES.length) % BG_STYLES.length];
}

// Resolve hex → rgb triplet string (cache-free, small inputs).
function rgbOf(hex: string): [number, number, number] {
  const clean = hex.replace("#", "");
  const full = clean.length === 3 ? clean.split("").map((c) => c + c).join("") : clean;
  const v = parseInt(full, 16);
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
}

function rgba(hex: string, alpha: number): string {
  const [r, g, b] = rgbOf(hex);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

interface SceneBackgroundProps {
  style: BgStyle;
  palette: ScenePalette;
  seed: number;
  durationInFrames: number;
}

export const SceneBackground: React.FC<SceneBackgroundProps> = ({
  style,
  palette,
  seed,
  durationInFrames,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // Crossfade in/out so consecutive scene backgrounds blend, never pop.
  const fadeIn = interpolate(frame, [0, 12], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const fadeOut = interpolate(
    frame,
    [Math.max(12, durationInFrames - 12), durationInFrames],
    [1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
  );
  const opacity = Math.min(fadeIn, fadeOut);

  const base = `linear-gradient(160deg, ${palette.bgBase} 0%, ${palette.bgAlt} 58%, ${palette.bgBase} 100%)`;

  return (
    <AbsoluteFill style={{ background: base, opacity }}>
      {style === "aurora" && <Aurora palette={palette} seed={seed} frame={frame} fps={fps} />}
      {style === "mesh" && <Mesh palette={palette} seed={seed} frame={frame} fps={fps} />}
      {style === "beams" && <Beams palette={palette} seed={seed} frame={frame} fps={fps} />}
      {style === "particles" && <Particles palette={palette} seed={seed} frame={frame} fps={fps} />}
      {style === "grid-pulse" && <GridPulse palette={palette} frame={frame} fps={fps} />}
      {style === "orbits" && <Orbits palette={palette} seed={seed} frame={frame} fps={fps} />}
      {style === "waves" && <Waves palette={palette} frame={frame} fps={fps} />}
      {style === "grain" && <Grain palette={palette} seed={seed} frame={frame} fps={fps} />}
    </AbsoluteFill>
  );
};

// --------------------------------------------------------------------------
// 1. Aurora — soft glowing blobs drifting on sine paths (the premium look).
// --------------------------------------------------------------------------
const Aurora: React.FC<{ palette: ScenePalette; seed: number; frame: number; fps: number }> = ({
  palette, seed, frame, fps,
}) => {
  const rnd = seededRandom(seed);
  const blobs = Array.from({ length: 5 }, (_, i) => ({
    size: 520 + rnd() * 620,
    cx: 12 + rnd() * 76,
    cy: 12 + rnd() * 76,
    ax: 7 + rnd() * 12,           // horizontal amplitude (%)
    ay: 5 + rnd() * 9,            // vertical amplitude (%)
    sx: 1 / (5 + rnd() * 7),      // angular speeds (rad per frame-ish)
    sy: 1 / (6 + rnd() * 8),
    phase: rnd() * Math.PI * 2,
    color: i % 2 === 0 ? palette.glowA : palette.glowB,
    alpha: 0.16 + rnd() * 0.10,
  }));
  return (
    <>
      {blobs.map((b, i) => {
        const x = b.cx + Math.sin(frame * b.sx * (fps / 30) + b.phase) * b.ax;
        const y = b.cy + Math.cos(frame * b.sy * (fps / 30) + b.phase) * b.ay;
        return (
          <div
            key={i}
            style={{
              position: "absolute",
              left: `${x}%`,
              top: `${y}%`,
              width: b.size,
              height: b.size * 0.72,
              transform: "translate(-50%, -50%)",
              borderRadius: "50%",
              background: `radial-gradient(ellipse at center, ${rgba(b.color, b.alpha)} 0%, ${rgba(b.color, b.alpha * 0.4)} 38%, rgba(0,0,0,0) 68%)`,
            }}
          />
        );
      })}
      {/* Faint starfield for depth */}
      {Array.from({ length: 26 }, (_, i) => {
        const r = seededRandom(seed + 991 + i * 17)();
        const x = (seededRandom(seed + 13 + i * 31)() * 100);
        const y = r * 100;
        const tw = 0.25 + 0.35 * (0.5 + 0.5 * Math.sin(frame * 0.09 + i * 1.7));
        return (
          <div
            key={`s-${i}`}
            style={{
              position: "absolute",
              left: `${x}%`,
              top: `${y}%`,
              width: 3,
              height: 3,
              borderRadius: "50%",
              background: rgba(palette.text, tw * 0.5),
            }}
          />
        );
      })}
    </>
  );
};

// --------------------------------------------------------------------------
// 2. Mesh — dense drifting radial gradient field (v1's look, but per-palette
//    and with 6 moving centers instead of 3).
// --------------------------------------------------------------------------
const Mesh: React.FC<{ palette: ScenePalette; seed: number; frame: number; fps: number }> = ({
  palette, seed, frame, fps,
}) => {
  const t = frame / fps;
  const centers = Array.from({ length: 6 }, (_, i) => {
    const rnd = seededRandom(seed + i * 97);
    return {
      x: 15 + rnd() * 70 + Math.sin(t * (0.10 + rnd() * 0.15) + i) * 14,
      y: 15 + rnd() * 70 + Math.cos(t * (0.08 + rnd() * 0.12) + i * 2) * 16,
      color: i % 2 === 0 ? palette.glowA : palette.glowB,
      alpha: 0.10 + (i % 3) * 0.035,
      stop: 42 + (i % 3) * 10,
    };
  });
  const gradient = centers
    .map(
      (c) =>
        `radial-gradient(ellipse at ${c.x}% ${c.y}%, ${rgba(c.color, c.alpha)} 0%, transparent ${c.stop}%)`,
    )
    .join(", ");
  return (
    <>
      <AbsoluteFill style={{ background: gradient }} />
      <div
        style={{
          position: "absolute",
          inset: 0,
          backgroundImage: `linear-gradient(${rgba(palette.text, 0.025)} 1px, transparent 1px), linear-gradient(90deg, ${rgba(palette.text, 0.025)} 1px, transparent 1px)`,
          backgroundSize: "72px 72px",
          transform: `translateY(${Math.sin(t * 0.35) * 14}px)`,
        }}
      />
    </>
  );
};

// --------------------------------------------------------------------------
// 3. Beams — rotating conic glow + three sweeping diagonal light beams.
// --------------------------------------------------------------------------
const Beams: React.FC<{ palette: ScenePalette; seed: number; frame: number; fps: number }> = ({
  palette, seed, frame, fps,
}) => {
  const t = frame / fps;
  const rot = t * 7;
  const beams = [0, 1, 2].map((i) => {
    const rnd = seededRandom(seed + i * 57);
    return {
      speed: 26 + rnd() * 30,
      width: 150 + rnd() * 190,
      alpha: 0.10 + rnd() * 0.07,
      color: i % 2 === 0 ? palette.glowA : palette.glowB,
      delay: rnd() * 10,
      angle: 18 + rnd() * 10,
    };
  });
  return (
    <>
      {/* Rotating conic center glow */}
      <div
        style={{
          position: "absolute",
          left: "50%",
          top: "50%",
          width: 1500,
          height: 1500,
          transform: `translate(-50%, -50%) rotate(${rot}deg)`,
          background: `conic-gradient(from 0deg, ${rgba(palette.glowA, 0.07)}, transparent 25%, ${rgba(palette.glowB, 0.07)} 50%, transparent 75%, ${rgba(palette.glowA, 0.07)})`,
          borderRadius: "50%",
        }}
      />
      {beams.map((b, i) => {
        const cycle = (t * b.speed + b.delay * 60) % 2600;
        const x = -900 + cycle;
        return (
          <div
            key={i}
            style={{
              position: "absolute",
              left: x,
              top: -300,
              width: b.width,
              height: 1800,
              transform: `rotate(${b.angle}deg)`,
              background: `linear-gradient(90deg, transparent, ${rgba(b.color, b.alpha)} 45%, ${rgba(b.color, b.alpha * 0.5)} 55%, transparent)`,
            }}
          />
        );
      })}
    </>
  );
};

// --------------------------------------------------------------------------
// 4. Particles — parallax dot field with twinkle (dust / bokeh feel).
// --------------------------------------------------------------------------
const Particles: React.FC<{ palette: ScenePalette; seed: number; frame: number; fps: number }> = ({
  palette, seed, frame, fps,
}) => {
  const t = frame / fps;
  const dots = Array.from({ length: 44 }, (_, i) => {
    const rnd = seededRandom(seed + i * 41);
    const depth = 0.35 + rnd() * 0.65; // parallax depth
    return {
      x: rnd() * 100,
      y0: rnd() * 100,
      size: 3 + depth * 9,
      drift: 2.5 + depth * 6,      // px per second upward
      sway: 6 + rnd() * 14,
      swaySpeed: 0.4 + rnd() * 0.8,
      color: i % 3 === 0 ? palette.glowB : palette.glowA,
      alpha: 0.18 + depth * 0.42,
      twinkleSpeed: 1.5 + rnd() * 3,
    };
  });
  return (
    <>
      {/* Ground glow */}
      <div
        style={{
          position: "absolute",
          left: "50%",
          bottom: -420,
          width: 1700,
          height: 820,
          transform: "translateX(-50%)",
          borderRadius: "50%",
          background: `radial-gradient(ellipse at center, ${rgba(palette.glowA, 0.16)} 0%, transparent 65%)`,
        }}
      />
      {dots.map((d, i) => {
        const y = d.y0 - (d.drift * t) % 115;
        const yy = y < -8 ? y + 115 : y;
        const x = d.x + Math.sin(t * d.swaySpeed + i) * 0.6;
        const tw = d.alpha * (0.55 + 0.45 * Math.sin(t * d.twinkleSpeed + i * 2.4));
        return (
          <div
            key={i}
            style={{
              position: "absolute",
              left: `${x}%`,
              top: `${yy}%`,
              width: d.size,
              height: d.size,
              borderRadius: "50%",
              background: `radial-gradient(circle, ${rgba(d.color, tw)} 0%, ${rgba(d.color, tw * 0.4)} 60%, transparent 75%)`,
            }}
          />
        );
      })}
    </>
  );
};

// --------------------------------------------------------------------------
// 5. GridPulse — perspective synthwave grid + travelling scan line.
// --------------------------------------------------------------------------
const GridPulse: React.FC<{ palette: ScenePalette; frame: number; fps: number }> = ({
  palette, frame, fps,
}) => {
  const t = frame / fps;
  const horizon = 46; // % from top
  return (
    <>
      {/* Horizon glow */}
      <div
        style={{
          position: "absolute",
          left: "50%",
          top: `${horizon}%`,
          width: 1900,
          height: 420,
          transform: "translate(-50%, -50%)",
          borderRadius: "50%",
          background: `radial-gradient(ellipse at center, ${rgba(palette.glowA, 0.28)} 0%, ${rgba(palette.glowB, 0.10)} 45%, transparent 70%)`,
        }}
      />
      {/* Perspective floor grid */}
      <div
        style={{
          position: "absolute",
          left: "-60%",
          top: `${horizon}%`,
          width: "220%",
          height: "80%",
          backgroundImage: `linear-gradient(${rgba(palette.accent, 0.30)} 2px, transparent 2px), linear-gradient(90deg, ${rgba(palette.accent, 0.30)} 2px, transparent 2px)`,
          backgroundSize: "90px 70px",
          transform: `perspective(560px) rotateX(64deg) translateY(${-(t * 26) % 70}px)`,
          transformOrigin: "top center",
        }}
      />
      {/* Travelling scan line */}
      <div
        style={{
          position: "absolute",
          left: 0,
          top: `${horizon + ((t * 9) % 50)}%`,
          width: "100%",
          height: 3,
          background: `linear-gradient(90deg, transparent, ${rgba(palette.accent, 0.55)}, transparent)`,
        }}
      />
      {/* Ceiling echo grid, dimmer */}
      <div
        style={{
          position: "absolute",
          left: "-60%",
          bottom: `${100 - horizon}%`,
          width: "220%",
          height: "34%",
          backgroundImage: `linear-gradient(${rgba(palette.glowB, 0.12)} 2px, transparent 2px), linear-gradient(90deg, ${rgba(palette.glowB, 0.12)} 2px, transparent 2px)`,
          backgroundSize: "120px 90px",
          transform: `perspective(560px) rotateX(-64deg)`,
          transformOrigin: "bottom center",
        }}
      />
    </>
  );
};

// --------------------------------------------------------------------------
// 6. Orbits — concentric rotating rings with glow dots (tech / radar feel).
// --------------------------------------------------------------------------
const Orbits: React.FC<{ palette: ScenePalette; seed: number; frame: number; fps: number }> = ({
  palette, seed, frame, fps,
}) => {
  const t = frame / fps;
  const rings = [0, 1, 2, 3].map((i) => {
    const rnd = seededRandom(seed + i * 71);
    return {
      size: 340 + i * 200,
      speed: (i % 2 === 0 ? 1 : -1) * (4 + rnd() * 6),
      dotColor: i % 2 === 0 ? palette.accent : palette.glowB,
      ringAlpha: 0.16 + rnd() * 0.10,
      dotAngle: rnd() * 360,
    };
  });
  return (
    <>
      {/* Center core glow */}
      <div
        style={{
          position: "absolute",
          left: "50%",
          top: "50%",
          width: 460,
          height: 460,
          transform: "translate(-50%, -50%)",
          borderRadius: "50%",
          background: `radial-gradient(circle, ${rgba(palette.glowA, 0.24)} 0%, ${rgba(palette.glowB, 0.10)} 45%, transparent 70%)`,
        }}
      />
      {rings.map((r, i) => {
        const angle = t * r.speed + r.dotAngle;
        const dotX = 50 + 50 * Math.cos((angle * Math.PI) / 180);
        const dotY = 50 + 50 * Math.sin((angle * Math.PI) / 180);
        return (
          <div
            key={i}
            style={{
              position: "absolute",
              left: "50%",
              top: "50%",
              width: r.size,
              height: r.size,
              transform: "translate(-50%, -50%)",
              borderRadius: "50%",
              border: `1.5px solid ${rgba(palette.text, r.ringAlpha)}`,
            }}
          >
            {/* Glow dot riding the ring — halo first so it reads as
                "satellite on orbit", not stray dust. */}
            <div
              style={{
                position: "absolute",
                left: `${dotX}%`,
                top: `${dotY}%`,
                width: 44,
                height: 44,
                transform: "translate(-50%, -50%)",
                borderRadius: "50%",
                background: `radial-gradient(circle, ${rgba(r.dotColor, 0.35)} 0%, transparent 70%)`,
              }}
            />
            <div
              style={{
                position: "absolute",
                left: `${dotX}%`,
                top: `${dotY}%`,
                width: 14,
                height: 14,
                transform: "translate(-50%, -50%)",
                borderRadius: "50%",
                background: r.dotColor,
                boxShadow: `0 0 16px ${r.dotColor}, 0 0 34px ${r.dotColor}66`,
              }}
            />
          </div>
        );
      })}
    </>
  );
};

// --------------------------------------------------------------------------
// 7. Waves — layered flowing SVG sine ribbons over a glowing horizon.
// --------------------------------------------------------------------------
const Waves: React.FC<{ palette: ScenePalette; frame: number; fps: number }> = ({
  palette, frame, fps,
}) => {
  const t = frame / fps;
  const layers = [
    { yBase: 56, amp: 30, len: 860, speed: 0.30, color: palette.glowA, alpha: 0.26, height: 46 },
    { yBase: 66, amp: 38, len: 620, speed: -0.22, color: palette.glowB, alpha: 0.20, height: 40 },
    { yBase: 74, amp: 26, len: 1180, speed: 0.16, color: palette.glowA, alpha: 0.16, height: 34 },
    { yBase: 82, amp: 18, len: 1500, speed: -0.12, color: palette.glowB, alpha: 0.12, height: 26 },
  ];
  // Finer sampling (40px) + quadratic smoothing for silky curves.
  const pathFor = (yBase: number, amp: number, len: number, speed: number) => {
    const phase = t * speed * Math.PI * 2;
    let d = `M 0 ${(yBase + Math.sin(phase) * amp).toFixed(1)}`;
    for (let x = 40; x <= 1920; x += 40) {
      const y = yBase + Math.sin((x / len) * Math.PI * 2 + phase) * amp;
      d += ` L ${x} ${y.toFixed(1)}`;
    }
    d += ` L 1920 1080 L 0 1080 Z`;
    return d;
  };
  return (
    <>
      {/* Horizon glow behind the ribbons */}
      <div
        style={{
          position: "absolute",
          left: "50%",
          top: "44%",
          width: 1800,
          height: 760,
          transform: "translate(-50%, -50%)",
          borderRadius: "50%",
          background: `radial-gradient(ellipse at center, ${rgba(palette.glowA, 0.20)} 0%, ${rgba(palette.glowB, 0.10)} 42%, transparent 66%)`,
        }}
      />
      {/* Upper-atmosphere echo */}
      <div
        style={{
          position: "absolute",
          left: "-10%",
          top: "6%",
          width: "120%",
          height: "26%",
          background: `linear-gradient(180deg, transparent, ${rgba(palette.glowB, 0.08)}, transparent)`,
          transform: `translateY(${Math.sin(t * 0.4) * 16}px)`,
        }}
      />
      <svg
        viewBox="0 0 1920 1080"
        style={{ position: "absolute", inset: 0, width: "100%", height: "100%" }}
      >
        <defs>
          <linearGradient id="crest-grad" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor={rgba(palette.text, 0.02)} />
            <stop offset="50%" stopColor={rgba(palette.accent, 0.55)} />
            <stop offset="100%" stopColor={rgba(palette.text, 0.02)} />
          </linearGradient>
        </defs>
        {layers.map((l, i) => (
          <path
            key={i}
            d={pathFor(l.yBase, l.amp, l.len, l.speed)}
            fill={rgba(l.color, l.alpha)}
          />
        ))}
        {/* Glowing crest lines for definition */}
        {layers.map((l, i) => (
          <path
            key={`c-${i}`}
            d={pathFor(l.yBase, l.amp, l.len, l.speed)}
            fill="none"
            stroke="url(#crest-grad)"
            strokeWidth={i === 0 ? 3 : 2}
          />
        ))}
      </svg>
    </>
  );
};

// --------------------------------------------------------------------------
// 8. Grain — cinematic film-grain field with breathing color wash.
// --------------------------------------------------------------------------
const Grain: React.FC<{ palette: ScenePalette; seed: number; frame: number; fps: number }> = ({
  palette, seed, frame, fps,
}) => {
  const t = frame / fps;
  const breathe = 0.5 + 0.5 * Math.sin(t * 0.5);
  const dots = Array.from({ length: 40 }, (_, i) => {
    const rnd = seededRandom(seed + i * 113 + Math.floor(frame / 3) * 7);
    return { x: rnd() * 100, y: rnd() * 100, s: 2 + rnd() * 4, a: 0.05 + rnd() * 0.10 };
  });
  return (
    <>
      <div
        style={{
          position: "absolute",
          inset: 0,
          background: `radial-gradient(ellipse at ${35 + Math.sin(t * 0.22) * 10}% ${40 + Math.cos(t * 0.18) * 12}%, ${rgba(palette.glowA, 0.14 + breathe * 0.08)} 0%, transparent 58%)`,
        }}
      />
      <div
        style={{
          position: "absolute",
          inset: 0,
          background: `radial-gradient(ellipse at ${68 + Math.cos(t * 0.19) * 9}% ${64 + Math.sin(t * 0.24) * 10}%, ${rgba(palette.glowB, 0.10 + (1 - breathe) * 0.07)} 0%, transparent 55%)`,
        }}
      />
      {dots.map((d, i) => (
        <div
          key={i}
          style={{
            position: "absolute",
            left: `${d.x}%`,
            top: `${d.y}%`,
            width: d.s,
            height: d.s,
            borderRadius: "50%",
            background: rgba(palette.text, d.a),
          }}
        />
      ))}
      {/* Vignette */}
      <div
        style={{
          position: "absolute",
          inset: 0,
          background: "radial-gradient(ellipse at center, transparent 52%, rgba(0,0,0,0.42) 100%)",
        }}
      />
    </>
  );
};
