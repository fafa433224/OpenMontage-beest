// ---------------------------------------------------------------------------
// MediaLayer — footage behind a scene, looped correctly.
//
// Extracted from Explainer.tsx so there is exactly ONE implementation of the
// gapless-loop rule. It used to live only there, and the first version of
// BespokeScene re-derived it — incorrectly:
//
//   * the loop window was the whole SCENE duration, not the footage
//     *remaining after the trim start*, and
//   * `trimAfter` was never set.
//
// The result on a 20 s scene over an 8 s clip with a 1.7 s trim-in was
// "Compositor error: No frame found at position 8106" — a hard render
// failure, not a cosmetic one. One shared implementation is the fix; two
// implementations of the same rule is how this recurs.
//
// The rule:
//   window   = clipEnd - startFrom            (must be > 0)
//   loop     = round(window * fps) frames     (never the scene length,
//               and omitted entirely when the footage already covers it)
//   trimAfter= round(clipEnd * fps)           (so the reader cannot run past)
// ---------------------------------------------------------------------------

import React from "react";
import { AbsoluteFill, Loop, OffthreadVideo, useVideoConfig } from "remotion";

import { resolveAsset } from "../lib/resolveAsset";
import type { ScenePalette } from "./palettes";

function hexToRgb(hex: string): { r: number; g: number; b: number } {
  const clean = hex.replace("#", "");
  const full = clean.length === 3
    ? clean.split("").map((c) => c + c).join("")
    : clean;
  const value = parseInt(full, 16);
  return { r: (value >> 16) & 255, g: (value >> 8) & 255, b: value & 255 };
}

export function withAlpha(hex: string, alpha: number): string {
  const { r, g, b } = hexToRgb(hex);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export interface BackgroundVideoLayerProps {
  src: string;
  startFrom?: number;
  /** Total source duration in seconds — required for gapless looping. */
  clipDuration?: number;
  endAt?: number;
  loop?: boolean;
  playbackRate?: number;
  overlayOpacity?: number;
  palette?: ScenePalette;
  /** Vertical gradient scrim, for compiled scenes where type sits on top. */
  gradientScrim?: boolean;
  /**
   * Length of the scene this layer belongs to, in frames. Used to skip the
   * `<Loop>` wrapper when the footage already covers the whole scene.
   */
  sceneFrames?: number;
  children: React.ReactNode;
}

const Scrim: React.FC<{
  palette?: ScenePalette;
  overlayOpacity: number;
  gradient: boolean;
}> = ({ palette, overlayOpacity, gradient }) => {
  if (gradient && palette) {
    return (
      <AbsoluteFill
        style={{
          background:
            `linear-gradient(180deg, ${withAlpha(palette.bgBase, Math.min(0.95, overlayOpacity * 0.82))} 0%, ` +
            `${withAlpha(palette.bgBase, 0.9)} 62%, ${withAlpha(palette.bgBase, 0.97)} 100%)`,
        }}
      />
    );
  }
  return (
    <AbsoluteFill
      style={{
        background: palette
          ? withAlpha(palette.bgBase, overlayOpacity)
          : `rgba(15, 23, 42, ${overlayOpacity})`,
      }}
    />
  );
};

export const BackgroundVideoLayer: React.FC<BackgroundVideoLayerProps> = ({
  src,
  startFrom = 0,
  clipDuration,
  endAt,
  loop = true,
  playbackRate,
  overlayOpacity = 0.5,
  palette,
  gradientScrim = false,
  sceneFrames,
  children,
}) => {
  const { fps } = useVideoConfig();

  // Loop window = footage remaining after the trim start. Requires the source
  // duration (the props builder passes probed durations). Without it we render
  // plain — legacy props keep their old behaviour.
  const clipEnd = endAt ?? clipDuration;
  const windowSeconds =
    loop && clipEnd !== undefined && clipEnd - startFrom > 0.05
      ? clipEnd - startFrom
      : undefined;
  // A PRE-BAKED clip is already exactly as long as its scene: play it once,
  // start to finish. No Loop, no trim, so there is no loop phase for two
  // render workers to disagree about — this is the path that removes the
  // compositor race rather than retrying around it.
  const prepared =
    clipDuration !== undefined &&
    startFrom <= 0.001 &&
    (sceneFrames === undefined || Math.abs(clipDuration * fps - sceneFrames) < fps);
  if (prepared) {
    return (
      <AbsoluteFill style={{ overflow: "hidden" }}>
        <OffthreadVideo
          src={resolveAsset(src)}
          playbackRate={playbackRate}
          style={{ width: "100%", height: "100%", objectFit: "cover" }}
          muted
        />
        <Scrim palette={palette} overlayOpacity={overlayOpacity} gradient={gradientScrim} />
        {children}
      </AbsoluteFill>
    );
  }

  const windowFrames = windowSeconds
    ? Math.max(2, Math.round(windowSeconds * fps))
    : undefined;
  // Loop ONLY when the scene outruns the footage. A `<Loop>` wrapping a
  // `trimBefore` video is a per-frame-varying seek pattern on a shared
  // asset, and that is the construct Remotion's multi-worker compositor
  // races on (see the retry ladder in render_voiceover_video.py). If the
  // footage already covers the scene, playing it straight is both correct
  // and race-free.
  const loopFrames =
    windowFrames === undefined
      ? undefined
      : sceneFrames !== undefined && sceneFrames <= windowFrames
        ? undefined
        : windowFrames;

  const video = (
    <OffthreadVideo
      src={resolveAsset(src)}
      trimBefore={Math.round(startFrom * fps)}
      trimAfter={clipEnd !== undefined ? Math.round(clipEnd * fps) : undefined}
      playbackRate={playbackRate}
      style={{ width: "100%", height: "100%", objectFit: "cover" }}
      muted
    />
  );

  return (
    <AbsoluteFill style={{ overflow: "hidden" }}>
      {/* Loops over the REMAINING window so long cuts never freeze on a frame
          (the v2 "laggy" bug) and never seek past the source. */}
      {loopFrames ? <Loop durationInFrames={loopFrames}>{video}</Loop> : video}
      <Scrim palette={palette} overlayOpacity={overlayOpacity} gradient={gradientScrim} />
      {children}
    </AbsoluteFill>
  );
};

export default BackgroundVideoLayer;
