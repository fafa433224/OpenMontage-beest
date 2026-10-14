// ---------------------------------------------------------------------------
// BespokeScene — the runtime for compiled choreography.
//
// lib/motion_compiler.py produces a ChoreographySpec: an ordered depth stack
// of layers, each with its own motion verb / window / easing / stagger, plus
// a camera path, a texture treatment and a signature moment. This file is
// the other half of that contract — it renders ANY spec, so the choreography
// is authored by the compiler rather than selected from a component list.
//
// Why one generic interpreter instead of generated TSX
// ----------------------------------------------------
// Generated components must compile; a spec cannot fail to compile. The
// interpreter is reviewed, typed and tested once; the *variety* lives in the
// data. That is the same trade Fable makes (a code-writing agent over a
// deterministic execution layer) with the ladder moved one step earlier.
//
// Rules this file must keep:
//   * Deterministic — no Math.random, no Date.now. Parallel frame capture
//     renders identical frames on every worker.
//   * Transform/opacity (+clip-path/filter) only. Never animate layout.
//   * Every window is a FRACTION of scene duration (Fable fractional timing).
// ---------------------------------------------------------------------------

import React from "react";
import {
  AbsoluteFill,
  Easing,
  Img,
  Loop,
  OffthreadVideo,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

import { resolveAsset } from "../lib/resolveAsset";
import { BackgroundVideoLayer, withAlpha } from "./MediaLayer";

import {
  SceneBackground,
  type BgStyle,
} from "./SceneBackground";
import type { ScenePalette } from "./palettes";

// ---------------------------------------------------------------------------
// Spec types — mirror lib/motion_compiler.py (SPEC_VERSION 1)
// ---------------------------------------------------------------------------

export const SPEC_VERSION = 1;

export type MotionVerb =
  | "rise" | "drop" | "slide-left" | "slide-right"
  | "scale-in" | "scale-out" | "blur-in" | "blur-out"
  | "wipe-up" | "wipe-down" | "wipe-left" | "wipe-right"
  | "curtain-x" | "unfold-y" | "orbit-in" | "spiral-in"
  | "zoom-through" | "flip-y" | "flip-x" | "skew-in"
  | "drift-in" | "tumble-in" | "elastic-pop" | "gravity-drop"
  | "counter-slide" | "settle" | "typewriter" | "reveal-none";

export type EaseName =
  | "linear" | "easeInQuad" | "easeOutQuad" | "easeInOutQuad"
  | "easeOutCubic" | "easeInOutCubic" | "easeOutQuart"
  | "easeOutExpo" | "easeInOutExpo" | "easeOutBack"
  | "easeInOutBack" | "easeOutElastic" | "easeOutCirc"
  | "overshoot" | "anticipate" | "hold-snap";

export type LayerKind =
  | "kicker" | "headline" | "body" | "rule" | "accent-bar"
  | "orb" | "orb-ring" | "grid" | "panel" | "chip" | "number"
  | "quote-mark" | "glyph" | "scrim" | "progress";

export interface SpecLayer {
  id: string;
  kind: LayerKind;
  depth: number;
  enterAt: number;
  enterDur: number;
  exitAt: number | null;
  exitDur: number;
  verb: MotionVerb;
  ease: EaseName;
  distance: number;
  stagger: number;
  opacity: number;
  blend: string;
  typeTreatment: string;
  holdCenter: boolean;
  rotate: number;
  scale: number;
}

export interface CameraKey {
  at: number;
  zoom?: number;
  x?: number;
  y?: number;
  rotate?: number;
}

export interface ChoreographySpec {
  version: number;
  id: string;
  scheme: string;
  contentClass: string;
  durationSeconds: number;
  align: "left" | "center" | "right";
  columns: number;
  ribbonSegments: number;
  typeTreatment: string;
  palette: ScenePalette;
  camera: { path: string; keys: CameraKey[]; depthCoupling: number };
  layers: SpecLayer[];
  signature: { moment: string; at: number; intensity: number };
  texture: { treatment: string; amount: number };
  text: {
    kicker?: string;
    headline?: string;
    body?: string;
    stat?: string;
    subtitle?: string;
  };
  /** Optional footage behind a compiled scene (the hero b-roll path). */
  media?: {
    src: string;
    kind: "video" | "image";
    startSeconds?: number;
    sourceDuration?: number;
    overlay?: number;
  } | null;
  seed: number;
  bgStyle?: BgStyle;
}

// ---------------------------------------------------------------------------
// Easing — named curves so the Python side stays declarative
// ---------------------------------------------------------------------------

const EASE_FNS: Record<EaseName, (t: number) => number> = {
  linear: Easing.linear,
  easeInQuad: Easing.in(Easing.quad),
  easeOutQuad: Easing.out(Easing.quad),
  easeInOutQuad: Easing.inOut(Easing.quad),
  easeOutCubic: Easing.out(Easing.cubic),
  easeInOutCubic: Easing.inOut(Easing.cubic),
  easeOutQuart: Easing.out(Easing.poly(4)),
  easeOutExpo: Easing.out(Easing.exp),
  easeInOutExpo: Easing.inOut(Easing.exp),
  easeOutBack: Easing.out(Easing.back(1.7)),
  easeInOutBack: Easing.inOut(Easing.back(1.7)),
  easeOutElastic: Easing.out(Easing.elastic(1)),
  easeOutCirc: Easing.out(Easing.circle),
  overshoot: Easing.out(Easing.back(2.4)),
  anticipate: Easing.in(Easing.back(1.4)),
  // Fast settle then dead-still hold — reads as "snap", never as a drift.
  "hold-snap": Easing.out(Easing.poly(6)),
};

const easeOf = (name: string): ((t: number) => number) =>
  EASE_FNS[name as EaseName] ?? EASE_FNS.easeOutCubic;

// ---------------------------------------------------------------------------
// Motion verbs → transforms
//
// Each verb is a pure function of progress p (0 = off-screen, 1 = settled).
// The renderer never animates layout: only transform / opacity / clip-path /
// filter, which the compositor can do without a main-thread layout pass.
// ---------------------------------------------------------------------------

interface MotionState {
  opacity: number;
  transform: string;
  clipPath?: string;
  filter?: string;
}

/** Deterministic per-item jitter (mirrors gsap.utils.random's *look*
 *  without its non-determinism). */
const jitter = (seed: number, i: number, amp: number): number => {
  const x = Math.sin(seed * 12.9898 + i * 78.233) * 43758.5453;
  return (x - Math.floor(x) - 0.5) * 2 * amp;
};

const motionState = (
  verb: string,
  p: number,
  distance: number,
  rotate: number,
  scale: number,
  seed: number,
  itemIndex: number
): MotionState => {
  const q = 1 - p; // "how far from settled"
  const op = Math.max(0, Math.min(1, p));
  const r = rotate + jitter(seed, itemIndex, 1.5) * q;
  const s = 1 + (scale - 1) * (1 - p);
  const d = distance * q;
  const frame = (extra: string, o: number, extraFilter?: string): MotionState => ({
    opacity: o,
    transform: extra,
    filter: extraFilter,
  });

  switch (verb) {
    case "rise":
      return frame(`translate3d(0, ${d}px, 0) rotate(${r}deg) scale(${s})`, op);
    case "drop":
      return frame(`translate3d(0, ${-d}px, 0) rotate(${r}deg) scale(${s})`, op);
    case "slide-left":
      return frame(`translate3d(${d}px, 0, 0) rotate(${r}deg) scale(${s})`, op);
    case "slide-right":
      return frame(`translate3d(${-d}px, 0, 0) rotate(${r}deg) scale(${s})`, op);
    case "scale-in":
      return frame(`scale(${0.82 + 0.18 * p}) rotate(${r}deg)`, op);
    case "scale-out":
      return frame(`scale(${1.28 - 0.28 * p}) rotate(${r}deg)`, op);
    case "blur-in":
      return frame(
        `translate3d(0, ${d * 0.35}px, 0) scale(${1 + (1 - p) * 0.03})`,
        op,
        `blur(${(1 - p) * 14}px)`
      );
    case "blur-out":
      return frame(`scale(${1 + p * 0.06})`, op, `blur(${p * 10}px)`);
    case "wipe-up":
      return { opacity: op, transform: `translate3d(0, ${d * 0.5}px, 0)`, clipPath: `inset(${(1 - p) * 100}% 0% 0% 0%)` };
    case "wipe-down":
      return { opacity: op, transform: `translate3d(0, ${-d * 0.5}px, 0)`, clipPath: `inset(0% 0% ${(1 - p) * 100}% 0%)` };
    case "wipe-left":
      return { opacity: op, transform: "none", clipPath: `inset(0% ${(1 - p) * 100}% 0% 0%)` };
    case "wipe-right":
      return { opacity: op, transform: "none", clipPath: `inset(0% 0% 0% ${(1 - p) * 100}%)` };
    case "curtain-x":
      // Symmetric: two halves part from the centre. Compresses to a reveal.
      return frame(`scaleX(${Math.max(0.001, p)})`, Math.min(1, p * 1.6));
    case "unfold-y":
      return frame(`scaleY(${Math.max(0.001, p)})`, Math.min(1, p * 1.6));
    case "orbit-in": {
      const ang = (1 - p) * 78;
      const ox = Math.sin((ang * Math.PI) / 180) * d;
      const oy = (1 - Math.cos((ang * Math.PI) / 180)) * d * 0.5;
      return frame(`translate3d(${ox}px, ${oy}px, 0) rotate(${r}deg) scale(${s})`, op);
    }
    case "spiral-in": {
      const ang = (1 - p) * 200;
      const rad = q * d;
      return frame(
        `translate3d(${Math.cos((ang * Math.PI) / 180) * rad}px, ${Math.sin((ang * Math.PI) / 180) * rad}px, 0) rotate(${r + ang * 0.25}deg) scale(${s})`,
        op
      );
    }
    case "zoom-through":
      return frame(`scale(${1.55 - 0.55 * p}) translate3d(0, ${d * 0.2}px, 0)`, op, `blur(${(1 - p) * 6}px)`);
    case "flip-y":
      return frame(`perspective(1200px) rotateY(${(1 - p) * 92}deg) translate3d(0, ${d * 0.4}px, 0)`, op);
    case "flip-x":
      return frame(`perspective(1200px) rotateX(${(1 - p) * 92}deg) translate3d(0, ${d * 0.4}px, 0)`, op);
    case "skew-in":
      return frame(`skewX(${(1 - p) * -14}deg) translate3d(${d}px, 0, 0) rotate(${r}deg)`, op);
    case "drift-in":
      return frame(`translate3d(${-d}px, ${d * 0.3}px, 0) rotate(${r}deg) scale(${s})`, op);
    case "tumble-in":
      return frame(
        `translate3d(${jitter(seed, itemIndex, 120) * q}px, ${-d * 0.6}px, 0) rotate(${jitter(seed, itemIndex + 9, 22) * q}deg) scale(${0.9 + 0.1 * p})`,
        op
      );
    case "elastic-pop":
      return frame(`scale(${0.7 + 0.3 * p}) rotate(${r * 0.4}deg)`, op);
    case "gravity-drop":
      return frame(`translate3d(${jitter(seed, itemIndex, 40) * q}px, ${(-260 - d) * q}px, 0) rotate(${jitter(seed, itemIndex + 3, 12) * q}deg)`, op);
    case "counter-slide":
      // Deliberately inverts the slide direction so split scenes read as
      // counter-motion rather than a single push.
      return frame(`translate3d(${-d}px, 0, 0) rotate(${r}deg) scale(${s})`, op);
    case "settle":
      return frame(`scale(${1 + (1 - p) * 0.06}) translate3d(0, ${d * 0.25 * q}px, 0)`, op);
    case "typewriter":
      return { opacity: 1, transform: "none", clipPath: `inset(0% ${(1 - p) * 100}% 0% 0%)` };
    case "reveal-none":
      return { opacity: op, transform: "none" };
    default:
      return frame(`translate3d(0, ${d}px, 0)`, op);
  }
};

// ---------------------------------------------------------------------------
// Layer chrome — what each layer kind actually looks like
// ---------------------------------------------------------------------------

interface LayerBox {
  text: string;
  items: string[];
  fontSize: number;
  isTextlike: boolean;
}

const splitList = (body: string): string[] =>
  body
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.length > 0)
    .map((l) => l.replace(/^[-•*]\s*/, "").replace(/^\d+[.)]\s*/, ""));

const headlineSize = (text: string, treatment: string): number => {
  const len = Math.max(1, text.length);
  if (treatment === "oversized-crop") return Math.min(220, Math.max(96, 2600 / len));
  if (treatment === "plain") return Math.min(128, Math.max(58, 1500 / len));
  if (len < 22) return 116;
  if (len < 44) return 88;
  if (len < 80) return 66;
  return 52;
};

const layerBox = (layer: SpecLayer, spec: ChoreographySpec): LayerBox => {
  const t = spec.text || {};
  switch (layer.kind) {
    case "kicker":
      return { text: t.kicker || "", items: [], fontSize: 30, isTextlike: true };
    case "headline":
      return {
        text: t.headline || "",
        items: [],
        fontSize: headlineSize(
          t.headline || "",
          layer.typeTreatment !== "plain" ? layer.typeTreatment : spec.typeTreatment
        ),
        isTextlike: true,
      };
    case "body": {
      const body = t.body || "";
      const items = spec.contentClass === "list" ? splitList(body) : [];
      return {
        text: body,
        items,
        fontSize: items.length > 1 ? 40 : body.length > 200 ? 40 : 46,
        isTextlike: true,
      };
    }
    case "number":
      return { text: t.stat || extractNumber(t.headline || "") || "", items: [], fontSize: 240, isTextlike: true };
    case "chip": {
      const src = spec.contentClass === "list" ? splitList(t.body || "") : [];
      const items = src.length > 0 ? src : (t.kicker ? [t.kicker] : []);
      return { text: items[0] || "", items, fontSize: 30, isTextlike: items.length > 0 };
    }
    default:
      return { text: "", items: [], fontSize: 0, isTextlike: false };
  }
};

const extractNumber = (text: string): string => {
  const m = text.match(/[$€£]?\s?\d[\d.,]*\s?(?:%|x|×|k|m|b|bn|million|billion)?/i);
  return m ? m[0].trim() : "";
};

// ---------------------------------------------------------------------------
// Texture overlays
// ---------------------------------------------------------------------------

const TextureLayer: React.FC<{ treatment: string; amount: number; frame: number; palette: ScenePalette }> = ({
  treatment,
  amount,
  frame,
  palette,
}) => {
  if (treatment === "none") return null;
  const a = Math.max(0, Math.min(1, amount));

  if (treatment === "vignette") {
    return (
      <AbsoluteFill
        style={{
          background: `radial-gradient(ellipse at center, transparent ${58 - a * 16}%, rgba(0,0,0,${0.34 + a * 0.36}) 100%)`,
          pointerEvents: "none",
        }}
      />
    );
  }
  if (treatment === "scanline") {
    // Deterministic scan offset — a plain moving stripe, not random noise.
    const y = ((frame * 0.9) % 6) - 6;
    return (
      <AbsoluteFill
        style={{
          backgroundImage:
            "repeating-linear-gradient(180deg, rgba(255,255,255,0.045) 0px, rgba(255,255,255,0.045) 1px, transparent 1px, transparent 3px)",
          backgroundPosition: `0 ${y}px`,
          mixBlendMode: "overlay",
          opacity: 0.35 + a * 0.5,
          pointerEvents: "none",
        }}
      />
    );
  }
  if (treatment === "halation") {
    return (
      <AbsoluteFill
        style={{
          background: `radial-gradient(ellipse at 30% 25%, ${palette.glowA}${Math.round(a * 40).toString(16).padStart(2, "0")} 0%, transparent 62%)`,
          mixBlendMode: "screen",
          pointerEvents: "none",
        }}
      />
    );
  }
  if (treatment === "noise-blend") {
    return (
      <AbsoluteFill
        style={{
          backgroundImage:
            "repeating-conic-gradient(from 0deg at 50% 50%, rgba(255,255,255,0.03) 0deg 1deg, transparent 1deg 2deg)",
          mixBlendMode: "overlay",
          opacity: 0.4 + a * 0.4,
          pointerEvents: "none",
        }}
      />
    );
  }
  // grain-soft / grain-heavy — deterministic dot lattice, seeded by frame
  const step = treatment === "grain-heavy" ? 3 : 5;
  const jx = ((frame * 7) % step) - step / 2;
  const jy = ((frame * 11) % step) - step / 2;
  return (
    <AbsoluteFill
      style={{
        backgroundImage: `radial-gradient(rgba(255,255,255,0.10) 0.5px, transparent 0.6px)`,
        backgroundSize: `${step}px ${step}px`,
        backgroundPosition: `${jx}px ${jy}px`,
        mixBlendMode: "overlay",
        opacity: (treatment === "grain-heavy" ? 0.42 : 0.24) * (0.6 + a * 0.8),
        pointerEvents: "none",
      }}
    />
  );
};

// ---------------------------------------------------------------------------
// Signature moments — one deliberate gesture per scene
// ---------------------------------------------------------------------------

const SignatureMoment: React.FC<{
  moment: string;
  atFraction: number;
  intensity: number;
  frame: number;
  durationInFrames: number;
  palette: ScenePalette;
}> = ({ moment, atFraction, intensity, frame, durationInFrames, palette }) => {
  if (moment === "none") return null;
  const start = atFraction * durationInFrames;
  const span = Math.max(8, durationInFrames * 0.22);
  const p = (frame - start) / span;
  if (p < 0 || p > 1) return null;
  const k = Math.sin(Math.PI * p); // 0 → 1 → 0
  const I = Math.max(0, Math.min(1, intensity)) * k;

  switch (moment) {
    case "luma-sweep":
      return (
        <AbsoluteFill
          style={{
            background: `linear-gradient(105deg, transparent ${40 - I * 55}%, ${palette.text}${Math.round(I * 90).toString(16).padStart(2, "0")} ${50}%, transparent ${60 + I * 55}%)`,
            mixBlendMode: "screen",
            pointerEvents: "none",
          }}
        />
      );
    case "seam-split": {
      const h = 2 + I * 26;
      return (
        <AbsoluteFill style={{ pointerEvents: "none" }}>
          <div
            style={{
              position: "absolute",
              top: `calc(50% - ${h / 2}px)`,
              left: 0,
              right: 0,
              height: h,
              background: palette.accent,
              opacity: I * 0.75,
              filter: `blur(${I * 8}px)`,
            }}
          />
        </AbsoluteFill>
      );
    }
    case "shadow-cascade":
      return (
        <AbsoluteFill style={{ pointerEvents: "none" }}>
          {[0, 1, 2, 3].map((i) => (
            <div
              key={i}
              style={{
                position: "absolute",
                left: 0,
                right: 0,
                top: `${18 + i * 20}%`,
                height: 6,
                background: `linear-gradient(90deg, transparent, ${palette.bgBase}, transparent)`,
                opacity: I * (0.5 - i * 0.09),
                transform: `translateX(${(1 - k) * (i % 2 === 0 ? 180 : -180)}px)`,
              }}
            />
          ))}
        </AbsoluteFill>
      );
    case "counter-fade":
      return (
        <AbsoluteFill
          style={{
            background: palette.bgBase,
            opacity: I * 0.22,
            mixBlendMode: "multiply",
            pointerEvents: "none",
          }}
        />
      );
    case "focus-rack":
      return (
        <AbsoluteFill
          style={{
            backdropFilter: `blur(${I * 5}px)`,
            opacity: I * 0.9,
            pointerEvents: "none",
          }}
        />
      );
    case "accent-flare":
      return (
        <AbsoluteFill
          style={{
            background: `radial-gradient(circle at 72% 34%, ${palette.glowA}${Math.round(I * 130).toString(16).padStart(2, "0")} 0%, transparent 46%)`,
            mixBlendMode: "screen",
            pointerEvents: "none",
          }}
        />
      );
    case "type-shuffle": {
      const shift = (1 - k) * 26;
      return (
        <AbsoluteFill style={{ pointerEvents: "none" }}>
          <div
            style={{
              position: "absolute",
              inset: 0,
              background: palette.accent,
              opacity: I * 0.08,
              transform: `translate3d(${shift}px, 0, 0)`,
            }}
          />
          <div
            style={{
              position: "absolute",
              inset: 0,
              background: palette.glowB,
              opacity: I * 0.08,
              transform: `translate3d(${-shift}px, 0, 0)`,
            }}
          />
        </AbsoluteFill>
      );
    }
    case "parallax-burst":
      return (
        <AbsoluteFill
          style={{
            border: `${Math.round(I * 40)}px solid ${palette.accent}`,
            opacity: I * 0.16,
            transform: `scale(${1 + I * 0.06})`,
            boxSizing: "border-box",
            pointerEvents: "none",
          }}
        />
      );
    case "ribbon-draw":
      return (
        <AbsoluteFill style={{ pointerEvents: "none" }}>
          {[0, 1, 2].map((i) => (
            <div
              key={i}
              style={{
                position: "absolute",
                left: "-10%",
                width: `${20 + 80 * k}%`,
                top: `${26 + i * 22}%`,
                height: 3 + i * 2,
                background: i === 1 ? palette.accent : palette.glowA,
                opacity: I * 0.55,
                transform: `rotate(${-13 + i * 9}deg)`,
                filter: `blur(${(1 - k) * 2}px)`,
              }}
            />
          ))}
        </AbsoluteFill>
      );
    case "settle-bounce":
      return (
        <AbsoluteFill
          style={{
            transform: `scale(${1 + I * 0.012})`,
            pointerEvents: "none",
          }}
        />
      );
    case "chromatic-punch":
      return (
        <AbsoluteFill style={{ pointerEvents: "none" }}>
          <AbsoluteFill
            style={{
              background: palette.accent,
              opacity: I * 0.07,
              mixBlendMode: "screen",
              transform: `translate3d(${I * 9}px, 0, 0)`,
            }}
          />
          <AbsoluteFill
            style={{
              background: palette.glowB,
              opacity: I * 0.07,
              mixBlendMode: "screen",
              transform: `translate3d(${-I * 9}px, 0, 0)`,
            }}
          />
        </AbsoluteFill>
      );
    case "bloom-through":
      return (
        <AbsoluteFill
          style={{
            background: `radial-gradient(circle at center, ${palette.text}${Math.round(I * 70).toString(16).padStart(2, "0")} 0%, transparent 70%)`,
            mixBlendMode: "screen",
            pointerEvents: "none",
          }}
        />
      );
    default:
      return null;
  }
};

// ---------------------------------------------------------------------------
// Typographic treatments — same content, radically different reading
// ---------------------------------------------------------------------------

const applyTreatment = (
  treatment: string,
  palette: ScenePalette,
  isHeadline: boolean
): React.CSSProperties => {
  if (treatment === "none" || treatment === "plain") return {};
  if (!isHeadline) return {};
  switch (treatment) {
    case "outline-overlay":
      // Outlined type is a look, not a licence to be unreadable. Measured at
      // 0.133 p05-p95 luma separation on a dark section palette (the visual
      // audit flagged it before any human saw it), so the stroke carries a
      // dark halo to separate it from the background underneath.
      return {
        color: "transparent",
        WebkitTextStroke: `3px ${palette.text}`,
        textShadow: `0 0 22px ${palette.bgBase}, 0 2px 10px ${palette.bgBase}`,
      };
    case "boxed-lower-third":
      return {
        background: palette.glass,
        borderLeft: `8px solid ${palette.accent}`,
        padding: "22px 34px",
        backdropFilter: "blur(10px)",
      };
    case "oversized-crop":
      return { letterSpacing: "-0.045em", lineHeight: 0.92 };
    case "vertical-rail":
      return { writingMode: "vertical-rl" as React.CSSProperties["writingMode"], textOrientation: "mixed" };
    case "split-justify":
      return { textAlign: "justify", hyphens: "auto" };
    case "quote-plate":
      return {
        fontStyle: "italic",
        borderTop: `2px solid ${palette.glassEdge}`,
        borderBottom: `2px solid ${palette.glassEdge}`,
        padding: "26px 0",
      };
    default:
      return {};
  }
};

// ---------------------------------------------------------------------------
// Layer renderer
//
// Two families, deliberately separated:
//   * DECOR — full-bleed, absolutely positioned, parallaxed by depth. These
//     paint BEHIND the content so type legibility can never be a casualty of
//     a scheme's ambition. (Rendering the first version revealed a
//     `ribbon-weave` glyph landing as a stray bar inside the text stack.)
//   * CONTENT — the vertical stack the scheme orders. Sorted by the compiler
//     into a readable sequence, so structure varies while legibility does not.
// ---------------------------------------------------------------------------

const DECOR_KINDS = new Set<LayerKind>([
  "orb",
  "orb-ring",
  "grid",
  "panel",
  "scrim",
  "glyph",
  "quote-mark",
]);

const isDecor = (layer: SpecLayer): boolean => DECOR_KINDS.has(layer.kind);

const LayerView: React.FC<{
  layer: SpecLayer;
  spec: ChoreographySpec;
  fonts: { heading: string; body: string };
}> = ({ layer, spec, fonts }) => {
  const frame = useCurrentFrame();
  const { durationInFrames, width, height } = useVideoConfig();
  const D = Math.max(1, durationInFrames);
  const pal = spec.palette;
  const box = layerBox(layer, spec);
  const dScale = typeof width === "number" && width > 0 ? width / 1920 : 1;

  // Fractional windows → frames.
  const enterStart = layer.enterAt * D;
  const enterSpan = Math.max(1, layer.enterDur * D);
  const exitStart = layer.exitAt === null ? Infinity : layer.exitAt * D;
  const exitSpan = Math.max(1, layer.exitDur * D);

  const ease = easeOf(layer.ease);
  const raw = (frame - enterStart) / enterSpan;
  const p = ease(Math.max(0, Math.min(1, raw)));
  const exiting = frame >= exitStart;
  const exP = exiting ? ease(Math.max(0, Math.min(1, (frame - exitStart) / exitSpan))) : 0;

  // Parallax: deeper layers move less with the camera. Driven by depth.
  const camCoupling = spec.camera.depthCoupling * (layer.depth - 0.5) * 26;

  let state = motionState(
    layer.verb,
    p,
    layer.distance,
    layer.rotate,
    layer.scale,
    spec.seed,
    0
  );
  if (exiting) {
    const out = motionState(
      layer.verb === "wipe-up" || layer.verb === "wipe-left" ? "wipe-left" : "scale-out",
      exP,
      Math.min(60, Math.abs(layer.distance) || 40),
      0,
      1,
      spec.seed,
      0
    );
    state = {
      opacity: state.opacity * (1 - exP),
      transform: out.transform === "none" ? state.transform : `${state.transform} ${out.transform}`,
      clipPath: out.clipPath ?? state.clipPath,
      filter: state.filter,
    };
  }

  const items = box.items.length > 0 ? box.items : box.text ? [box.text] : [];
  if (!box.isTextlike || items.length === 0) {
    // Decorative layers have no text content — render their chrome below.
    items.length = 0;
  }

  const baseStyle: React.CSSProperties = {
    opacity: state.opacity * layer.opacity,
    transform: `${state.transform} translate3d(${camCoupling}px, 0, 0)`,
    clipPath: state.clipPath,
    filter: state.filter,
    mixBlendMode: (layer.blend === "normal" ? undefined : layer.blend) as React.CSSProperties["mixBlendMode"],
    willChange: "transform, opacity",
  };

  const alignStyle: React.CSSProperties = {
    textAlign: spec.align,
    alignItems:
      spec.align === "center" ? "center" : spec.align === "right" ? "flex-end" : "flex-start",
  };

  // ---- decorative chrome ------------------------------------------------
  const decor = (): React.ReactElement | null => {
    switch (layer.kind) {
      case "rule":
        return (
          <div
            style={{
              width: `${Math.max(6, 22 + 42 * p)}%`,
              height: 3,
              background: `linear-gradient(90deg, ${pal.accent}, ${pal.glowB})`,
              borderRadius: 2,
              transformOrigin: spec.align === "center" ? "center" : "left",
            }}
          />
        );
      case "accent-bar":
        // A short horizontal divider, not a free-floating tick. Rendering the
        // vertical 6x78 version centred in the stack read as an accident
        // overlapping the headline (caught by looking at the frame).
        return (
          <div
            style={{
              width: 76 + 46 * p,
              height: 5,
              borderRadius: 3,
              background: `linear-gradient(90deg, ${pal.accent}, ${pal.glowA})`,
              boxShadow: `0 0 22px ${pal.accent}55`,
              transformOrigin: spec.align === "right" ? "right center" : spec.align === "center" ? "center" : "left center",
            }}
          />
        );
      case "orb":
        return (
          <div
            style={{
              position: "absolute",
              width: width * 0.52,
              height: width * 0.52,
              left: spec.align === "right" ? -width * 0.1 : width * 0.48,
              top: -width * 0.14,
              borderRadius: "50%",
              background: `radial-gradient(circle, ${pal.glowA}55 0%, transparent 68%)`,
              filter: "blur(6px)",
            }}
          />
        );
      case "orb-ring":
        return (
          <div
            style={{
              position: "absolute",
              width: width * 0.76,
              height: width * 0.76,
              left: width * 0.5 - width * 0.38,
              top: height * 0.5 - width * 0.38,
              borderRadius: "50%",
              border: `1.5px solid ${pal.glassEdge}`,
              transform: `rotate(${frame * 0.12}deg)`,
            }}
          />
        );
      case "grid":
        return (
          <div
            style={{
              position: "absolute",
              inset: 0,
              backgroundImage:
                `linear-gradient(${pal.text}14 1px, transparent 1px), linear-gradient(90deg, ${pal.text}14 1px, transparent 1px)`,
              backgroundSize: `${Math.round(width / 14)}px ${Math.round(width / 14)}px`,
            }}
          />
        );
      case "panel":
        return (
          <div
            style={{
              position: "absolute",
              inset: `${height * 0.06}px ${width * 0.04}px`,
              background: pal.glass,
              border: `1px solid ${pal.glassEdge}`,
              backdropFilter: "blur(14px)",
            }}
          />
        );
      case "scrim":
        return (
          <div
            style={{
              position: "absolute",
              inset: 0,
              background: `linear-gradient(180deg, transparent 0%, ${pal.bgBase} 55%, ${pal.bgBase} 100%)`,
            }}
          />
        );
      case "progress": {
        const fill = Math.max(0, Math.min(1, p));
        return (
          <div
            style={{
              width: "58%",
              height: 10,
              borderRadius: 5,
              background: `${pal.text}22`,
              overflow: "hidden",
            }}
          >
            <div
              style={{
                width: `${fill * 100}%`,
                height: "100%",
                background: `linear-gradient(90deg, ${pal.accent}, ${pal.glowA})`,
              }}
            />
          </div>
        );
      }
      case "quote-mark":
        return <div style={{ fontSize: 190, lineHeight: 0.7, color: pal.accent, opacity: 0.8 }}>&ldquo;</div>;
      case "glyph":
        // A ribbon, not a scratch. The first version was a 6px flat line at
        // 0.5 opacity and read as a rendering artifact (caught by looking at
        // the frame); a tapered gradient band reads as designed.
        return (
          <div
            style={{
              position: "absolute",
              width: `${46 + 54 * p}%`,
              height: 14 + 10 * layer.depth,
              left: `${4 + (spec.seed % 4) * 6}%`,
              // Kept out of the 26–74% vertical band: that is where the
              // content stack lives, and a ribbon crossing a headline reads
              // as a defect. (Seen in the first full render — a ribbon cut
              // through the "47%" numeral.)
              top: `${(spec.seed % 2) === 0 ? 8 + (spec.seed % 3) * 4 : 76 + (spec.seed % 3) * 3}%`,
              background: `linear-gradient(90deg, transparent 0%, ${pal.glowA} 22%, ${pal.accent} 62%, transparent 100%)`,
              opacity: 0.34 + 0.3 * layer.depth,
              borderRadius: 999,
              filter: "blur(0.4px)",
              boxShadow: `0 0 34px ${pal.glowA}44`,
            }}
          />
        );
      default:
        return null;
    }
  };

  // ---- text layers ------------------------------------------------------
  const isHeadline = layer.kind === "headline" || layer.kind === "number";
  const treatment = applyTreatment(
    layer.typeTreatment !== "plain" ? layer.typeTreatment : spec.typeTreatment,
    pal,
    isHeadline
  );

  const weight = layer.kind === "number" ? 800 : isHeadline ? 800 : layer.kind === "kicker" ? 600 : 400;
  const fontFamily = isHeadline || layer.kind === "number" ? fonts.heading : fonts.body;

  // ---- decorative layers: full-bleed, behind the content ----------------
  if (isDecor(layer)) {
    const el = decor();
    if (!el) return null;
    return (
      <div
        style={{
          ...baseStyle,
          position: "absolute",
          inset: 0,
          pointerEvents: "none",
          zIndex: 0,
        }}
      >
        {el}
      </div>
    );
  }

  const staggerFrames = layer.stagger * D;

  // ---- content layers: flex items in the scene stack --------------------
  const rowStyle: React.CSSProperties = {
    ...baseStyle,
    display: "flex",
    flexDirection: "column",
    gap: 10 * dScale,
    width: "100%",
    ...alignStyle,
    zIndex: 2,
  };

  // A chrome layer (rule / accent-bar / progress) with no copy renders as
  // its own row — an intentional divider between content rows, not an
  // absolute mark floating over the type.
  if (items.length === 0) {
    const el = decor();
    if (!el) return null;
    return <div style={rowStyle}>{el}</div>;
  }

  return (
    <div style={rowStyle}>
      {items.map((item, i) => {
        const ip = layer.stagger > 0
          ? ease(Math.max(0, Math.min(1, (frame - enterStart - i * staggerFrames) / enterSpan)))
          : p;
        const ist = motionState(
          layer.verb,
          ip,
          layer.distance,
          layer.rotate,
          layer.scale,
          spec.seed,
          i
        );
        return (
          <div
            key={`${layer.id}-${i}`}
            style={{
              opacity: ist.opacity,
              transform: ist.transform,
              clipPath: ist.clipPath,
              filter: ist.filter,
              fontFamily,
              fontWeight: weight,
              // Static letterSpacing only — never an animated layout prop.
              letterSpacing:
                layer.kind === "kicker" || layer.kind === "chip" ? "0.22em" : "-0.02em",
              textTransform:
                layer.kind === "kicker" || layer.kind === "chip" ? "uppercase" : "none",
              fontSize: box.fontSize * dScale,
              lineHeight: layer.kind === "body" ? 1.45 : 1.06,
              maxWidth: layer.kind === "body" ? "86%" : "100%",
              color:
                layer.kind === "kicker" || layer.kind === "chip" || layer.kind === "body"
                  ? pal.muted
                  : pal.text,
              ...treatment,
            }}
          >
            {layer.kind === "chip" ? (
              <span
                style={{
                  display: "inline-block",
                  padding: `${8 * dScale}px ${18 * dScale}px`,
                  borderRadius: 999,
                  background: pal.glass,
                  border: `1px solid ${pal.glassEdge}`,
                  fontSize: (box.fontSize + 2) * dScale,
                }}
              >
                {item}
              </span>
            ) : layer.kind === "number" ? (
              <span
                style={{
                  background: `linear-gradient(180deg, ${pal.text} 0%, ${pal.accent} 100%)`,
                  WebkitBackgroundClip: "text",
                  backgroundClip: "text",
                  color: "transparent",
                }}
              >
                {item}
              </span>
            ) : (
              item
            )}
          </div>
        );
      })}
    </div>
  );
};

// ---------------------------------------------------------------------------
// Camera
// ---------------------------------------------------------------------------

const cameraTransform = (
  keys: CameraKey[],
  at: number
): { zoom: number; x: number; y: number; rotate: number } => {
  if (!keys || keys.length === 0) return { zoom: 1, x: 0, y: 0, rotate: 0 };
  if (keys.length === 1 || at <= keys[0].at) {
    const k = keys[0];
    return { zoom: k.zoom ?? 1, x: k.x ?? 0, y: k.y ?? 0, rotate: k.rotate ?? 0 };
  }
  const last = keys[keys.length - 1];
  if (at >= last.at) return { zoom: last.zoom ?? 1, x: last.x ?? 0, y: last.y ?? 0, rotate: last.rotate ?? 0 };
  for (let i = 0; i < keys.length - 1; i += 1) {
    const a = keys[i];
    const b = keys[i + 1];
    if (at >= a.at && at <= b.at) {
      const span = Math.max(1e-6, b.at - a.at);
      const t = (at - a.at) / span;
      const e = Easing.inOut(Easing.quad)(t);
      return {
        zoom: (a.zoom ?? 1) + ((b.zoom ?? 1) - (a.zoom ?? 1)) * e,
        x: (a.x ?? 0) + ((b.x ?? 0) - (a.x ?? 0)) * e,
        y: (a.y ?? 0) + ((b.y ?? 0) - (a.y ?? 0)) * e,
        rotate: (a.rotate ?? 0) + ((b.rotate ?? 0) - (a.rotate ?? 0)) * e,
      };
    }
  }
  return { zoom: 1, x: 0, y: 0, rotate: 0 };
};

// ---------------------------------------------------------------------------
// The scene
// ---------------------------------------------------------------------------

export interface BespokeSceneProps {
  spec: ChoreographySpec;
  fonts?: { heading: string; body: string };
  /** Cross-fade the whole scene in/out at its edges (cut softness). */
  softEdges?: boolean;
  /**
   * `false` (default) when the Explainer hosts this scene: the host already
   * provides the animated SceneBackground, the CameraDrift and the
   * SceneTransition, so painting them again would double-apply and the
   * camera would fight itself. `true` renders a complete, self-contained
   * scene (preview composition, HyperFrames-style standalone use).
   */
  standalone?: boolean;
}

export const BespokeScene: React.FC<BespokeSceneProps> = ({
  spec,
  fonts = { heading: "Space Grotesk", body: "Space Grotesk" },
  softEdges,
  standalone = false,
}) => {
  const frame = useCurrentFrame();
  const { durationInFrames, width, height } = useVideoConfig();
  const D = Math.max(1, durationInFrames);
  const at = Math.max(0, Math.min(1, frame / D));

  const cam = cameraTransform(spec.camera?.keys ?? [], at);
  const fade = softEdges ?? standalone;
  const edge = fade
    ? Math.min(
        1,
        interpolate(frame, [0, Math.round(D * 0.06)], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        }),
        interpolate(frame, [D - Math.round(D * 0.06), D], [1, 0], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        })
      )
    : 1;

  const camTransform =
    `translate3d(${-cam.x * width}px, ${-cam.y * height}px, 0) ` +
    `scale(${cam.zoom}) rotate(${cam.rotate}deg)`;

  return (
    <AbsoluteFill
      style={{
        backgroundColor: standalone ? spec.palette.bgBase : "transparent",
        overflow: "hidden",
      }}
    >
      {/* Animated brand background — the same vocabulary the rest of the
          pipeline uses, so a bespoke scene still belongs to the channel.
          Skipped when hosted: the Explainer paints it for every cut. */}
      {(!standalone || !spec.media) && (
        <SceneBackground
          style={(spec.bgStyle ?? "aurora") as BgStyle}
          palette={spec.palette}
          seed={spec.seed ?? 0}
          durationInFrames={D}
        />
      )}

      {/* Footage under a compiled scene, via the shared MediaLayer so the
          gapless-loop rule has exactly one implementation. (Re-deriving it
          here broke a 20 s scene over an 8 s clip with a hard
          "No frame found at position" compositor error.) The gradient scrim
          is not decoration: a headline on raw footage is a legibility
          coin-flip, and the whole point of compiling is readable words. */}
      {spec.media && spec.media.kind !== "image" && (
        <BackgroundVideoLayer
          src={spec.media.src}
          startFrom={spec.media.startSeconds ?? 0}
          clipDuration={spec.media.sourceDuration}
          loop
          overlayOpacity={spec.media.overlay ?? 0.5}
          palette={spec.palette}
          gradientScrim
          sceneFrames={D}
        >
          <AbsoluteFill />
        </BackgroundVideoLayer>
      )}
      {/* Image media has no loop window to get wrong. */}
      {spec.media?.kind === "image" && (
        <AbsoluteFill style={{ overflow: "hidden" }}>
          <Img
            src={resolveAsset(spec.media.src)}
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
          />
          <AbsoluteFill
            style={{
              background:
                `linear-gradient(180deg, ${withAlpha(spec.palette.bgBase, Math.min(0.95, (spec.media.overlay ?? 0.5) * 0.82))} 0%, ` +
                `${withAlpha(spec.palette.bgBase, 0.9)} 62%, ${withAlpha(spec.palette.bgBase, 0.97)} 100%)`,
            }}
          />
        </AbsoluteFill>
      )}

      {/* Texture pass — a grade on the PLATE, applied before anything is
          typeset on it. It used to render last, i.e. over the words, and the
          cost was measurable: a scanline pass at 0.388 over 46px body copy
          in a half-scale render chewed the strokes into mid-grey. Real film
          grain sits over the whole frame, but it is luminance-neutral noise;
          a line-based treatment that only ever darkens is not, so it goes
          under the type where it can only help the look. */}
      <TextureLayer
        treatment={spec.texture?.treatment ?? "none"}
        amount={spec.texture?.amount ?? 0.3}
        frame={frame}
        palette={spec.palette}
      />

      <AbsoluteFill style={{ transform: camTransform, opacity: edge }}>
        {/* Depth stack: back layers first. Layer order is authored by the
            compiler's scheme, not by DOM accident. */}
        <AbsoluteFill
          style={{
            padding: `${Math.round(height * 0.14)}px ${Math.round(width * 0.10)}px`,
            display: "flex",
            flexDirection: "column",
            justifyContent: "center",
            alignItems:
              spec.align === "center" ? "center" : spec.align === "right" ? "flex-end" : "flex-start",
          }}
        >
          {/* Decor paints first (behind); content rows paint above it, so a
              scheme can never bury the words it is carrying. */}
          {spec.layers.filter(isDecor).map((layer) => (
            <LayerView key={layer.id} layer={layer} spec={spec} fonts={fonts} />
          ))}
          <div
            style={{
              position: "relative",
              display: "flex",
              flexDirection: "column",
              gap: 16 * (typeof width === "number" && width > 0 ? width / 1920 : 1),
              width: "100%",
              alignItems:
                spec.align === "center" ? "center" : spec.align === "right" ? "flex-end" : "flex-start",
            }}
          >
            {spec.layers.filter((l) => !isDecor(l)).map((layer) => (
              <LayerView key={layer.id} layer={layer} spec={spec} fonts={fonts} />
            ))}
          </div>
        </AbsoluteFill>
      </AbsoluteFill>

      <SignatureMoment
        moment={spec.signature?.moment ?? "none"}
        atFraction={spec.signature?.at ?? 0.4}
        intensity={spec.signature?.intensity ?? 0.6}
        frame={frame}
        durationInFrames={D}
        palette={spec.palette}
      />

    </AbsoluteFill>
  );
};

export default BespokeScene;

// ---------------------------------------------------------------------------
// Runtime guard — the renderer-side half of lib/motion_compiler.lint_spec()
//
// The compiler lints before writing props, but props can also arrive from a
// hand-edited file or an older project. A malformed spec must never render
// as a broken frame: it returns a reason string and the host falls back down
// the ladder to a stock component. Cheap by design — it is on the hot path
// for every bespoke cut.
// ---------------------------------------------------------------------------

export function validateSpec(spec: unknown): string | null {
  if (!spec || typeof spec !== "object") return "spec is not an object";
  const s = spec as Partial<ChoreographySpec>;
  if (s.version !== SPEC_VERSION) {
    return `spec.version ${String(s.version)} != ${SPEC_VERSION}`;
  }
  if (!Array.isArray(s.layers) || s.layers.length === 0) return "spec.layers empty";
  if (!s.palette || typeof s.palette.bgBase !== "string") return "spec.palette missing";
  const dur = s.durationSeconds;
  if (typeof dur !== "number" || !(dur > 0)) return "spec.durationSeconds invalid";
  for (const l of s.layers) {
    if (!l || typeof l.kind !== "string" || typeof l.verb !== "string") {
      return "spec layer missing kind/verb";
    }
    if (typeof l.enterAt !== "number" || l.enterAt < 0 || l.enterAt > 1) {
      return `spec layer ${l.id ?? "?"} enterAt out of range`;
    }
  }
  const keys = s.camera?.keys;
  if (keys !== undefined && !Array.isArray(keys)) return "spec.camera.keys invalid";
  return null;
}
