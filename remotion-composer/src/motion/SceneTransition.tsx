import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import type { ScenePalette } from "./palettes";

// ---------------------------------------------------------------------------
// SceneTransition — per-cut entrance/exit choreography + camera drift +
// full-screen luma sweep. v1 had NONE of this: cuts hard-popped between
// Sequences, which is exactly the "robotic, nothing changes" feel.
//
// How it composes (inside each cut's Sequence):
//   <CameraDrift>            — continuous subtle zoom/pan (alive, not static)
//     <SceneBackground/>     — per-cut animated background (separate file)
//     <SceneTransition>      — entrance + exit choreography of the CONTENT
//       {content}
//     </SceneTransition>
//   </CameraDrift>
//   <TransitionSweep/>       — accent luma bar flashing across at cut start
//
// Performance contract: transforms + opacity only. NO filter:blur() (see the
// fb7cf87 orb fix), no clip-path animation on large trees (wipe uses a cheap
// inset mask on a wrapper div which Chrome rasterizes once).
// ---------------------------------------------------------------------------

export const TRANSITION_STYLES = [
  "slide-left",
  "slide-right",
  "slide-up",
  "zoom-punch",
  "wipe",
  "flip",
  "glitch",
  "rise",
] as const;

export type TransitionStyle = (typeof TRANSITION_STYLES)[number];

export function transitionForIndex(i: number): TransitionStyle {
  return TRANSITION_STYLES[((i % TRANSITION_STYLES.length) + TRANSITION_STYLES.length) % TRANSITION_STYLES.length];
}

// Frames used for enter/exit choreography. 0.36s at 30fps — snappy but visible.
const T_IN = 11;
const T_OUT = 9;

interface SceneTransitionProps {
  style: TransitionStyle;
  durationInFrames: number;
  children: React.ReactNode;
}

export const SceneTransition: React.FC<SceneTransitionProps> = ({
  style,
  durationInFrames,
  children,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const outStart = Math.max(T_IN, durationInFrames - T_OUT);

  // Entrance: springy 0→1 (with overshoot where the style calls for it)
  const enter = spring({ frame, fps, config: { damping: 16, stiffness: 110, mass: 0.9 } });
  // Exit: smooth ease-down 1→0
  const exit = interpolate(frame, [outStart, durationInFrames], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: (x) => 1 - Math.pow(1 - x, 2.2),
  });

  let transform = "none";
  let clipPath: string | undefined;
  let extraOpacity = 1;

  switch (style) {
    case "slide-left":
      transform = `translateX(${(1 - enter) * 130}px) translateX(${(1 - exit) * -130}px)`;
      extraOpacity = Math.min(enter > 0.02 ? 1 : enter * 12, exit);
      break;
    case "slide-right":
      transform = `translateX(${(1 - enter) * -130}px) translateX(${(1 - exit) * 130}px)`;
      extraOpacity = Math.min(enter > 0.02 ? 1 : enter * 12, exit);
      break;
    case "slide-up":
      transform = `translateY(${(1 - enter) * 120}px) translateY(${(1 - exit) * -90}px)`;
      extraOpacity = Math.min(enter > 0.02 ? 1 : enter * 12, exit);
      break;
    case "zoom-punch": {
      const sIn = 1.16 - 0.16 * enter; // settle from 1.16 → 1
      const sOut = 0.90 + 0.10 * exit; // 1 → 0.90
      transform = `scale(${sIn * sOut})`;
      extraOpacity = exit;
      break;
    }
    case "wipe": {
      // Cheap inset mask: reveal left→right on enter, consume left→right on exit.
      const reveal = Math.min(enter * 1.15, 1) * 100;
      const consume = (1 - exit) * 100;
      clipPath = `inset(0 ${Math.max(0, 100 - reveal)}% 0 ${Math.min(consume, 100) > 0 && exit < 1 ? Math.min(consume * 0.0, 0) : 0}%)`;
      // Exit: slide the mask origin instead — simplest correct: fade+slide on exit
      transform = `translateX(${(1 - exit) * -60}px)`;
      extraOpacity = exit;
      break;
    }
    case "flip": {
      const rIn = (1 - enter) * 16; // degrees
      const rOut = (1 - exit) * -12;
      transform = `perspective(1400px) rotateY(${rIn + rOut}deg) scale(${0.94 + 0.06 * enter * exit})`;
      extraOpacity = Math.min(enter > 0.05 ? 1 : enter * 10, exit);
      break;
    }
    case "glitch": {
      // Decaying jitter + skew — reads as a digital glitch settle.
      const j = (1 - enter);
      const jitter = j > 0.12 ? Math.sin(frame * 2.4) * 26 * j : 0;
      const skew = j > 0.12 ? Math.sin(frame * 1.7) * 2.2 * j : 0;
      transform = `translateX(${jitter}px) skewX(${skew}deg) translateY(${(1 - exit) * 40}px)`;
      extraOpacity = Math.min(enter > 0.04 ? 1 : enter * 14, exit);
      break;
    }
    case "rise":
    default:
      transform = `translateY(${(1 - enter) * 64}px) scale(${0.97 + 0.03 * enter}) translateY(${(1 - exit) * 30}px)`;
      extraOpacity = Math.min(enter, exit);
      break;
  }

  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        transform,
        clipPath,
        opacity: Math.max(0, Math.min(1, extraOpacity)),
        willChange: "transform, opacity",
      }}
    >
      {children}
    </div>
  );
};

// --------------------------------------------------------------------------
// CameraDrift — continuous slow zoom + sway over the whole scene so even a
// "static" card never looks frozen. Applied to background + content together.
// --------------------------------------------------------------------------
export const CameraDrift: React.FC<{
  durationInFrames: number;
  seed: number;
  children: React.ReactNode;
}> = ({ durationInFrames, seed, children }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const t = frame / fps;
  const total = durationInFrames / fps;

  // Slow push-in over the scene lifetime (1.5% zoom max) + gentle sway.
  const progress = interpolate(frame, [0, Math.max(1, durationInFrames)], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const zoom = 1.015 + progress * 0.02;
  const dir = seed % 2 === 0 ? 1 : -1;
  const swayX = Math.sin(t * 0.35 + seed) * 7 * dir;
  const swayY = Math.cos(t * 0.28 + seed) * 5;
  // Guard: never drift more than the zoom headroom allows (no edge reveal).
  const _ = total; // referenced for clarity; zoom math above is frame-based
  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        transform: `scale(${zoom}) translate(${swayX.toFixed(2)}px, ${swayY.toFixed(2)}px)`,
        willChange: "transform",
      }}
    >
      {children}
    </div>
  );
};

// --------------------------------------------------------------------------
// TransitionSweep — a skewed accent gradient bar that flashes across the
// frame in the first ~0.3s of a cut. This is the "editor" luma-wipe feel and
// it visually sells the cut change even when content is similar. Pairs with
// the whoosh SFX fired at the same timestamp.
// --------------------------------------------------------------------------
export const TransitionSweep: React.FC<{
  palette: ScenePalette;
  seed: number;
  enabled?: boolean;
}> = ({ palette, seed, enabled = true }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  if (!enabled) return null;

  const D = 10; // frames
  if (frame > D) return null;

  const p = frame / D; // 0→1
  const dir = seed % 2 === 0 ? 1 : -1;
  // Bar travels fully across; opacity peaks mid-sweep.
  const travel = interpolate(p, [0, 1], dir === 1 ? [-0.35, 1.35] : [1.35, -0.35]);
  const opacity = Math.sin(p * Math.PI) * 0.9;

  return (
    <div style={{ position: "absolute", inset: 0, overflow: "hidden", pointerEvents: "none" }}>
      <div
        style={{
          position: "absolute",
          top: "-25%",
          height: "150%",
          left: `${travel * 100}%`,
          width: 320,
          transform: `translateX(-50%) rotate(${14 * dir}deg)`,
          background: `linear-gradient(90deg, transparent, ${palette.accent}22 20%, ${palette.accent}88 50%, ${palette.accent}22 80%, transparent)`,
          opacity,
        }}
      />
      {/* Thin leading edge line for crispness */}
      <div
        style={{
          position: "absolute",
          top: "-25%",
          height: "150%",
          left: `${(travel + 0.045 * dir) * 100}%`,
          width: 5,
          transform: `translateX(-50%) rotate(${14 * dir}deg)`,
          background: palette.text,
          opacity: opacity * 0.5,
        }}
      />
    </div>
  );
};
