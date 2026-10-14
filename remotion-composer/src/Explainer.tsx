import {
  AbsoluteFill,
  Audio,
  Img,
  Loop,
  OffthreadVideo,
  Sequence,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { loadSpaceGrotesk } from "./lib/localFonts";
import { TextCard } from "./components/TextCard";
import { StatCard } from "./components/StatCard";
import { CalloutBox } from "./components/CalloutBox";
import { ComparisonCard } from "./components/ComparisonCard";
import { BarChart } from "./components/charts/BarChart";
import { LineChart } from "./components/charts/LineChart";
import { PieChart } from "./components/charts/PieChart";
import { KPIGrid } from "./components/charts/KPIGrid";
import { ProgressBar } from "./components/ProgressBar";
import { CaptionOverlay, WordCaption, type CaptionStyleConfig } from "./components/CaptionOverlay";
import { BrandOverlay } from "./components/BrandOverlay";
import { SectionTitle } from "./components/SectionTitle";
import { StatReveal } from "./components/StatReveal";
import { HeroTitle } from "./components/HeroTitle";
import { AnimeScene } from "./components/AnimeScene";
import type { CameraMotion } from "./components/AnimeScene";
import { TerminalScene } from "./components/TerminalScene";
import type { TerminalStep } from "./components/TerminalScene";
import { ScreenshotScene } from "./components/ScreenshotScene";
import type { ScreenshotStep } from "./components/ScreenshotScene";
import { ProviderChip } from "./components/ProviderChip";
import { resolveAsset } from "./lib/resolveAsset";
import type { ParticleType } from "./components/ParticleOverlay";
import { resolveTheme, type ThemeConfig, type BrandOverlayConfig } from "./theme";
import {
  paletteByIndex,
  effectivePalettes,
  paletteForSectionIn,
  hashString,
  type ScenePalette,
} from "./motion/palettes";
import {
  SceneBackground,
  bgStyleForIndex,
  type BgStyle,
} from "./motion/SceneBackground";
import {
  SceneTransition,
  CameraDrift,
  TransitionSweep,
  transitionForCut,
  type TransitionStyle,
} from "./motion/SceneTransition";

// Load Space Grotesk font for cinematic typography
const { fontFamily } = loadSpaceGrotesk(["400", "700"]);

// ---------------------------------------------------------------------------
// Animated Background — Gradient Mesh + Floating Orbs
// ---------------------------------------------------------------------------

// Parse hex color to RGB components
function hexToRgb(hex: string): { r: number; g: number; b: number } {
  const clean = hex.replace("#", "");
  const bigint = parseInt(clean.length === 3
    ? clean.split("").map(c => c + c).join("")
    : clean, 16);
  return { r: (bigint >> 16) & 255, g: (bigint >> 8) & 255, b: bigint & 255 };
}

// Detect if a color is "light" (for choosing grid/overlay treatment)
function isLightColor(hex: string): boolean {
  const { r, g, b } = hexToRgb(hex);
  return (r * 299 + g * 587 + b * 114) / 1000 > 128;
}

// Scrim painted behind a hero title. It has to wash *away* from the theme's
// text color: a dark scrim under a light theme's dark text drops the pair to
// ~3.4:1, which is the same legibility bug in reverse.
function heroScrim(theme: ThemeConfig): string {
  const { r, g, b } = hexToRgb(
    isLightColor(theme.backgroundColor) ? "#FFFFFF" : "#0F172A"
  );
  return (
    `radial-gradient(ellipse at center, rgba(${r},${g},${b},0.35) 0%, ` +
    `rgba(${r},${g},${b},0.55) 100%)`
  );
}

// Darken/lighten a color by mixing toward black or white
function shiftColor(hex: string, amount: number): string {
  const { r, g, b } = hexToRgb(hex);
  const clamp = (v: number) => Math.max(0, Math.min(255, Math.round(v)));
  if (amount < 0) {
    // Darken
    const f = 1 + amount;
    return `rgb(${clamp(r * f)}, ${clamp(g * f)}, ${clamp(b * f)})`;
  }
  // Lighten
  return `rgb(${clamp(r + (255 - r) * amount)}, ${clamp(g + (255 - g) * amount)}, ${clamp(b + (255 - b) * amount)})`;
}

// ---------------------------------------------------------------------------
// BaseBackground — the neutral deep layer beneath every scene.
// Per-scene animated backgrounds (SceneBackground) crossfade in/out at cut
// boundaries; this base shows through for those few blend frames, giving
// every cut an intentional visual "beat" instead of a hard pop.
// ---------------------------------------------------------------------------

const BaseBackground: React.FC<{ theme: ThemeConfig }> = ({ theme }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const bg = theme.backgroundColor;
  const { r: bgR, g: bgG, b: bgB } = hexToRgb(bg);
  const t = frame / fps;
  return (
    <AbsoluteFill
      style={{
        background: `
          radial-gradient(ellipse at ${38 + Math.sin(t * 0.11) * 8}% ${42 + Math.cos(t * 0.09) * 8}%,
            rgba(${bgR}, ${bgG}, ${bgB}, 0.9) 0%, transparent 62%),
          linear-gradient(165deg, ${bg} 0%, ${shiftColor(bg, isLightColor(bg) ? -0.04 : 0.04)} 100%)
        `,
      }}
    />
  );
};

// ---------------------------------------------------------------------------
// Types — aligned with edit_decisions artifact schema
// ---------------------------------------------------------------------------

interface Cut {
  id: string;
  source: string;
  in_seconds: number;
  out_seconds: number;
  layer?: string;
  type?: string;
  // Component-specific props
  text?: string;
  stat?: string;
  subtitle?: string;
  callout_type?: "info" | "warning" | "tip" | "quote";
  title?: string;
  // Video source trim — seek to this point in the source before playback.
  // Defaults to 0 (play from beginning). Use this instead of in_seconds for source trimming.
  source_in_seconds?: number;
  // Comparison props
  leftLabel?: string;
  rightLabel?: string;
  leftValue?: string;
  rightValue?: string;
  // Chart props
  chartData?: any[];
  chartSeries?: any[];
  chartColors?: string[];
  chartAnimation?: string;
  donut?: boolean;
  centerLabel?: string;
  centerValue?: string;
  showGrid?: boolean;
  showValues?: boolean;
  showLegend?: boolean;
  showMarkers?: boolean;
  xLabel?: string;
  yLabel?: string;
  columns?: 2 | 3 | 4;
  // Progress bar props
  progress?: number;
  progressLabel?: string;
  progressColor?: string;
  progressAnimation?: string;
  progressSegments?: any[];
  // Hero title props (when used as scene, not overlay)
  heroSubtitle?: string;
  // Styling overrides
  backgroundColor?: string;
  cardBackgroundColor?: string; // Inner card surface (comparison); defaults to theme.surfaceColor
  backgroundImage?: string; // AI-generated or stock image rendered behind the component
  backgroundVideo?: string; // Video clip rendered behind the component (takes priority over backgroundImage)
  backgroundVideoStart?: number; // Seek position in seconds for background video (default 0)
  backgroundVideoDuration?: number; // Source clip length in seconds — enables gapless looping (lag fix)
  backgroundVideoEnd?: number; // Clamp playback to this source position in seconds (optional)
  backgroundVideoLoop?: boolean; // Loop the clip when the cut outlasts it (default true — lag fix)
  backgroundVideoRate?: number; // Playback rate for ambient slow-mo (optional)
  backgroundOverlay?: number; // Opacity of dark overlay on backgroundImage/backgroundVideo (0-1, default 0.55)
  color?: string;
  accentColor?: string;
  fontSize?: number;
  // Animation & transitions
  animation?: string;
  transition_in?: string;
  transition_out?: string;
  transition_duration?: number;
  transform?: {
    animation?: string;
    scale?: number;
    position?: string | { x: number; y: number };
  };
  // Anime scene props (type: "anime_scene")
  images?: string[];
  particles?: ParticleType;
  particleColor?: string;
  particleCount?: number;
  particleIntensity?: number;
  vignette?: boolean;
  lightingFrom?: string;
  lightingTo?: string;
  // Terminal scene props (type: "terminal_scene")
  steps?: TerminalStep[];
  terminalTitle?: string;
  prompt?: string;
  // Screenshot scene props (type: "screenshot_scene")
  screenshotSteps?: ScreenshotStep[];
  screenshotSize?: { width: number; height: number };
  cursorStartAt?: [number, number];
  // --- Creative direction (motion design v2) ---
  /** Which section of the script this cut belongs to (drives the palette). */
  sectionIndex?: number;
  /** Total number of sections (drives palette spread). */
  sectionCount?: number;
  /** Explicit palette override (index into SECTION_PALETTES). */
  paletteIndex?: number;
  /** Animated background style for this cut (aurora|mesh|beams|...). */
  bgStyle?: BgStyle;
  /** Entrance/exit choreography style (slide-left|zoom-punch|...). */
  transition?: TransitionStyle;
  /** Show the accent luma sweep at cut start (default true). */
  sweep?: boolean;
  /** Kicker label above text (e.g. "PART 2 — PIPELINE"). */
  kicker?: string;
  /** Text alignment rotation for text_card scenes. */
  align?: "left" | "center" | "right";
  /** Words to highlight in the accent color on text_card scenes. */
  accentWords?: string[];
}

interface Overlay {
  type: "section_title" | "stat_reveal" | "hero_title" | "provider_chip";
  in_seconds: number;
  out_seconds: number;
  text?: string;
  subtitle?: string;
  accentColor?: string;
  position?: string;
  // provider_chip
  providers?: string[];
  cycleSeconds?: number;
  label?: string;
}

interface AudioLayer {
  src: string;
  volume?: number;
}

interface AudioConfig {
  narration?: AudioLayer;
  music?: AudioLayer & {
    fadeInSeconds?: number;
    fadeOutSeconds?: number;
    /** Start playback from this offset in seconds (skip quiet intros).
     *  Use the audio_energy tool to find the optimal offset. */
    offsetSeconds?: number;
    /** Loop the music if it's shorter than the video duration. */
    loop?: boolean;
  };
}

/** One sound-effect cue: play `src` starting at `atSeconds`. */
interface SfxTrack {
  src: string;
  atSeconds: number;
  volume?: number;
  /** How long the cue window stays open (default 4s; long risers need more). */
  durationSeconds?: number;
}

export interface ExplainerProps {
  [key: string]: unknown;
  cuts: Cut[];
  overlays?: Overlay[];
  captions?: WordCaption[];
  audio?: AudioConfig;
  /** Sound-effect cues (whooshes on cuts, impacts on heroes, chimes...). */
  sfx?: SfxTrack[];
  /** Caption style pack from the creative director (look/pacing/position). */
  captionStyle?: CaptionStyleConfig;
  /** Project-specific palettes derived from theme colors (replaces the
   *  fixed six-palette ring when present — see lib/creative_director.py). */
  customPalettes?: unknown;
  /** Per-project seed for transition/palette fallback selection. */
  creativeSeed?: number;
  /** Transition timing multiplier: 0.7 snappy / 1 standard / 1.4 cinematic. */
  transitionSpeed?: number;
  /** Channel brand identity layer (watermark/wordmark) — emitted when the
   *  run is channel-branded (lib/brand_profile.py brand_overlay_props). */
  brandOverlay?: BrandOverlayConfig;
}

// ---------------------------------------------------------------------------
// Image Extensions
// ---------------------------------------------------------------------------

const IMAGE_EXTENSIONS = [".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"];
const VIDEO_EXTENSIONS = [".mp4", ".mov", ".webm", ".avi", ".mkv"];

function isImage(source: string): boolean {
  const lower = source.toLowerCase();
  return IMAGE_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

function isVideo(source: string): boolean {
  const lower = source.toLowerCase();
  return VIDEO_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

// ---------------------------------------------------------------------------
// Cinematic vignette overlay
// ---------------------------------------------------------------------------

const Vignette: React.FC = () => (
  <AbsoluteFill
    style={{
      background:
        "radial-gradient(ellipse at center, transparent 50%, rgba(0,0,0,0.6) 100%)",
      pointerEvents: "none",
    }}
  />
);

// ---------------------------------------------------------------------------
// Enhanced Image Scene — spring physics, parallax, variety
// ---------------------------------------------------------------------------

const ImageScene: React.FC<{
  src: string;
  animation?: string;
  /** Duration of the ENCLOSING cut in frames — makes the exit crossfade run
   *  at the right time instead of at composition end. */
  sceneDurationFrames?: number;
}> = ({
  src,
  animation,
  sceneDurationFrames,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const sceneFrames = sceneDurationFrames ?? durationInFrames;

  // Smooth spring fade-in
  const fadeIn = spring({ frame, fps, config: { damping: 18, stiffness: 80 } });

  // Fade-out for crossfade effect
  const fadeOutStart = sceneFrames - 8;
  const fadeOut = interpolate(frame, [fadeOutStart, sceneFrames], [1, 0.3], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  let scale = 1;
  let translateX = 0;
  let translateY = 0;
  const anim = animation || "zoom-in";

  // Progress with easing — smoother than linear
  const progress = interpolate(frame, [0, sceneFrames], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  if (anim === "zoom-in") {
    scale = 1 + progress * 0.18;
  } else if (anim === "zoom-out") {
    scale = 1.18 - progress * 0.18;
  } else if (anim === "pan-left") {
    translateX = interpolate(progress, [0, 1], [40, -40]);
    scale = 1.15;
  } else if (anim === "pan-right") {
    translateX = interpolate(progress, [0, 1], [-40, 40]);
    scale = 1.15;
  } else if (anim === "ken-burns" || anim === "ken-burns-slow-zoom") {
    // Cinematic Ken Burns: gentle zoom + diagonal drift
    scale = 1 + progress * 0.22;
    translateX = interpolate(progress, [0, 1], [0, -25]);
    translateY = interpolate(progress, [0, 1], [0, -15]);
  } else if (anim === "parallax") {
    // Subtle parallax — foreground moves faster
    translateY = interpolate(progress, [0, 1], [15, -15]);
    scale = 1.1;
  }
  // "static" or "none" → just display

  return (
    <AbsoluteFill style={{ overflow: "hidden", background: "#0F172A" }}>
      <Img
        src={resolveAsset(src)}
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
          opacity: fadeIn * fadeOut,
          transform: `scale(${scale}) translate(${translateX}px, ${translateY}px)`,
          willChange: "transform, opacity",
        }}
      />
      <Vignette />
    </AbsoluteFill>
  );
};

// ---------------------------------------------------------------------------
// Enhanced Video Scene
// ---------------------------------------------------------------------------

const VideoScene: React.FC<{
  src: string;
  startFrom?: number;
  transitionIn?: string;
  transitionOut?: string;
  transitionDuration?: number;
  sceneDurationSeconds: number;
  backgroundColor?: string;
}> = ({
  src,
  startFrom = 0,
  transitionIn,
  transitionOut,
  transitionDuration,
  sceneDurationSeconds,
  backgroundColor = "#0F172A",
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const durationInFrames = Math.max(1, Math.round(sceneDurationSeconds * fps));

  const hardIn = ["cut", "none"].includes((transitionIn || "").toLowerCase());
  const hardOut = ["cut", "none"].includes((transitionOut || "").toLowerCase());
  const transitionFrames = Math.max(
    1,
    Math.round((transitionDuration ?? 8 / fps) * fps),
  );
  const fadeIn = hardIn
    ? 1
    : interpolate(frame, [0, transitionFrames], [0, 1], {
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
      });
  const fadeOutStart = Math.max(0, durationInFrames - transitionFrames);
  const fadeOut = hardOut
    ? 1
    : interpolate(frame, [fadeOutStart, durationInFrames], [1, 0], {
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
      });

  return (
    <AbsoluteFill style={{ background: backgroundColor }}>
      <OffthreadVideo
        src={resolveAsset(src)}
        startFrom={Math.round(startFrom * fps)}
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
          opacity: fadeIn * fadeOut,
        }}
        muted
      />
      <Vignette />
    </AbsoluteFill>
  );
};

// ---------------------------------------------------------------------------
// Scene renderer — maps cut type / source to the right component
// ---------------------------------------------------------------------------

// Background image layer — renders a stock/AI image behind data components.
// v2: palette-tinted scrim (instead of flat navy) + cinematic Ken Burns with
// diagonal drift, so b-roll feels like an editor placed it, not a slide deck.
const BackgroundImageLayer: React.FC<{
  src: string;
  overlayOpacity?: number;
  palette?: ScenePalette;
  children: React.ReactNode;
}> = ({ src, overlayOpacity = 0.5, palette, children }) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();

  // Cinematic Ken Burns: zoom + slow diagonal drift, direction varies by seed.
  const progress = interpolate(frame, [0, durationInFrames], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const seed = hashString(src);
  const dirX = seed % 2 === 0 ? 1 : -1;
  const bgScale = 1.08 + progress * 0.08;
  const bgX = dirX * interpolate(progress, [0, 1], [-18, 18]);
  const bgY = interpolate(progress, [0, 1], [10, -10]);

  const scrim = palette
    ? withAlpha(palette.bgBase, overlayOpacity)
    : `rgba(15, 23, 42, ${overlayOpacity})`;

  return (
    <AbsoluteFill style={{ overflow: "hidden" }}>
      {/* Background image with cinematic drift */}
      <Img
        src={resolveAsset(src)}
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
          transform: `scale(${bgScale}) translate(${bgX}px, ${bgY}px)`,
          willChange: "transform",
        }}
      />
      {/* Palette-tinted scrim for readability */}
      <AbsoluteFill style={{ background: scrim }} />
      {/* Component content on top */}
      {children}
    </AbsoluteFill>
  );
};

// Background video layer — plays a stock clip behind component content with
// a palette-tinted scrim.
//
// v2.1 LAG FIX: this layer used to pass only `startFrom`, so when a cut ran
// longer than the remaining footage (8s synth clips started up to 5s in,
// text cards running 20s+) OffthreadVideo froze on the last frame — the
// background visibly "stuck" mid-scene, which read as a laggy render. Fix:
//   * the builder passes `backgroundVideoDuration` (it knows/probes it),
//   * we wrap the video in Remotion's <Loop> over the remaining window so
//     long cuts cycle the clip instead of freezing,
//   * `endAt` clamps playback to a known-good portion of the source,
//   * `playbackRate` allows cinematic ambient slow-mo.
const BackgroundVideoLayer: React.FC<{
  src: string;
  startFrom?: number;
  /** Total source duration in seconds (enables gapless looping). */
  clipDuration?: number;
  endAt?: number;
  loop?: boolean;
  playbackRate?: number;
  overlayOpacity?: number;
  palette?: ScenePalette;
  children: React.ReactNode;
}> = ({
  src,
  startFrom = 0,
  clipDuration,
  endAt,
  loop = true,
  playbackRate,
  overlayOpacity = 0.5,
  palette,
  children,
}) => {
  const { fps } = useVideoConfig();

  const scrim = palette
    ? withAlpha(palette.bgBase, overlayOpacity)
    : `rgba(15, 23, 42, ${overlayOpacity})`;

  // Loop window = footage remaining after the trim start. Requires the
  // source duration (passed by the builder). Without it we render plain —
  // legacy props keep their old behavior.
  const clipEnd = endAt ?? clipDuration;
  const windowSeconds =
    loop && clipEnd !== undefined && clipEnd - startFrom > 0.05
      ? clipEnd - startFrom
      : undefined;
  const loopFrames = windowSeconds
    ? Math.max(2, Math.round(windowSeconds * fps))
    : undefined;

  const video = (
    <OffthreadVideo
      src={resolveAsset(src)}
      trimBefore={Math.round(startFrom * fps)}
      trimAfter={clipEnd !== undefined ? Math.round(clipEnd * fps) : undefined}
      playbackRate={playbackRate}
      style={{
        width: "100%",
        height: "100%",
        objectFit: "cover",
      }}
      muted
    />
  );

  return (
    <AbsoluteFill style={{ overflow: "hidden" }}>
      {/* Background video — loops over the remaining window so long cuts
          never freeze on a frame (the v2 "laggy" bug) */}
      {loopFrames ? <Loop durationInFrames={loopFrames}>{video}</Loop> : video}
      {/* Palette-tinted scrim for readability */}
      <AbsoluteFill style={{ background: scrim }} />
      {/* Component content on top */}
      {children}
    </AbsoluteFill>
  );
};

/** hex → rgba() string (used for palette scrims). */
function withAlpha(hex: string, alpha: number): string {
  const { r, g, b } = hexToRgb(hex);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

const SceneRenderer: React.FC<{
  cut: Cut;
  theme: ThemeConfig;
  palette: ScenePalette;
  durationInFrames: number;
}> = ({ cut, theme, palette, durationInFrames }) => {
  // Wrap component with background video or image if specified
  const maybeWrapWithBg = (element: React.ReactElement) => {
    if (cut.backgroundVideo) {
      return (
        <BackgroundVideoLayer
          src={cut.backgroundVideo}
          startFrom={cut.backgroundVideoStart ?? 0}
          clipDuration={cut.backgroundVideoDuration}
          endAt={cut.backgroundVideoEnd}
          loop={cut.backgroundVideoLoop ?? true}
          playbackRate={cut.backgroundVideoRate}
          overlayOpacity={cut.backgroundOverlay ?? 0.5}
          palette={palette}
        >
          {element}
        </BackgroundVideoLayer>
      );
    }
    if (cut.backgroundImage && cut.type !== "screenshot_scene") {
      return (
        <BackgroundImageLayer
          src={cut.backgroundImage}
          overlayOpacity={cut.backgroundOverlay ?? 0.5}
          palette={palette}
        >
          {element}
        </BackgroundImageLayer>
      );
    }
    return element;
  };

  // Resolve the scene element based on cut type, then wrap with backgroundImage if set
  // Use transparent bg so the animated scene background shows through
  // When no explicit backgroundColor on the cut, inherit from theme
  const rawBg = (cut.backgroundImage || cut.backgroundVideo) ? "transparent" : (cut.backgroundColor || theme.surfaceColor);
  const bgColor = (rawBg === theme.backgroundColor || rawBg === "#0F172A" || rawBg === "#0f172a") ? "transparent" : rawBg;
  const textColor = cut.color || palette.text;
  const accent = cut.accentColor || palette.accent;

  // Explicit component types — use palette-derived defaults for colors
  if (cut.type === "text_card" && cut.text) {
    return maybeWrapWithBg(
      <TextCard
        text={cut.text}
        fontSize={cut.fontSize}
        color={textColor}
        backgroundColor={bgColor}
        align={cut.align}
        kicker={cut.kicker}
        accentColor={accent}
        accentWords={cut.accentWords}
        glass={palette.glass}
        glassEdge={palette.glassEdge}
        mutedColor={palette.muted}
        sceneDurationFrames={durationInFrames}
      />
    );
  }
  if (cut.type === "stat_card" && cut.stat) {
    return maybeWrapWithBg(
      <StatCard
        stat={cut.stat}
        subtitle={cut.subtitle}
        accentColor={accent}
        backgroundColor={bgColor}
        glass={palette.glass}
        glassEdge={palette.glassEdge}
      />
    );
  }
  if (cut.type === "callout" && cut.text) {
    return maybeWrapWithBg(
      <CalloutBox
        text={cut.text} type={cut.callout_type} title={cut.title}
        borderColor={accent} backgroundColor={cut.backgroundColor || theme.surfaceColor}
        textColor={textColor} containerBackgroundColor={bgColor}
      />
    );
  }
  if (cut.type === "comparison" && cut.leftLabel && cut.rightLabel && cut.leftValue && cut.rightValue) {
    return maybeWrapWithBg(
      <ComparisonCard
        leftLabel={cut.leftLabel} rightLabel={cut.rightLabel}
        leftValue={cut.leftValue} rightValue={cut.rightValue}
        title={cut.title} backgroundColor={bgColor} textColor={textColor}
        cardBackgroundColor={cut.cardBackgroundColor || theme.surfaceColor}
      />
    );
  }
  if (cut.type === "hero_title" && cut.text) {
    return maybeWrapWithBg(
      <HeroTitle
        title={cut.text}
        subtitle={cut.heroSubtitle || cut.subtitle}
        accentColor={accent}
        textColor={textColor}
        subtitleColor={palette.muted}
        scrimBackground="none"
        noScrim
        sceneDurationFrames={durationInFrames}
      />
    );
  }
  if (cut.type === "terminal_scene" && cut.steps) {
    return maybeWrapWithBg(
      <TerminalScene
        title={cut.terminalTitle || "Terminal"}
        steps={cut.steps as TerminalStep[]}
        prompt={cut.prompt}
        accentColor={accent}
        backgroundColor={bgColor || theme.backgroundColor}
      />
    );
  }
  if (cut.type === "screenshot_scene" && cut.backgroundImage && cut.screenshotSteps) {
    return (
      <ScreenshotScene
        backgroundImage={cut.backgroundImage}
        backgroundSize={cut.screenshotSize}
        steps={cut.screenshotSteps as ScreenshotStep[]}
        accentColor={accent}
        cursorStartAt={cut.cursorStartAt}
      />
    );
  }

  // --- Chart types — use theme.chartColors as default palette ---
  if (cut.type === "bar_chart" && cut.chartData) {
    return maybeWrapWithBg(
      <BarChart
        data={cut.chartData} title={cut.title} colors={cut.chartColors || theme.chartColors}
        animationStyle={(cut.chartAnimation as any) || "grow-up"}
        showGrid={cut.showGrid} showValues={cut.showValues} backgroundColor={bgColor}
        textColor={textColor}
        sceneDurationFrames={durationInFrames}
      />
    );
  }
  if (cut.type === "line_chart" && cut.chartSeries) {
    return maybeWrapWithBg(
      <LineChart
        series={cut.chartSeries} title={cut.title} colors={cut.chartColors || theme.chartColors}
        animationStyle={(cut.chartAnimation as any) || "draw"}
        showGrid={cut.showGrid} showMarkers={cut.showMarkers} showLegend={cut.showLegend}
        xLabel={cut.xLabel} yLabel={cut.yLabel} backgroundColor={bgColor}
        textColor={textColor}
        sceneDurationFrames={durationInFrames}
      />
    );
  }
  if (cut.type === "pie_chart" && cut.chartData) {
    return maybeWrapWithBg(
      <PieChart
        data={cut.chartData} title={cut.title} colors={cut.chartColors || theme.chartColors}
        animationStyle={(cut.chartAnimation as any) || "expand"}
        donut={cut.donut} centerLabel={cut.centerLabel} centerValue={cut.centerValue}
        showLegend={cut.showLegend} backgroundColor={bgColor}
        textColor={textColor}
        sceneDurationFrames={durationInFrames}
      />
    );
  }
  if (cut.type === "kpi_grid" && cut.chartData) {
    return maybeWrapWithBg(
      <KPIGrid
        metrics={cut.chartData} title={cut.title} columns={cut.columns}
        colors={cut.chartColors || theme.chartColors} animationStyle={(cut.chartAnimation as any) || "count-up"}
        backgroundColor={bgColor}
        textColor={textColor}
        sceneDurationFrames={durationInFrames}
      />
    );
  }
  if (cut.type === "progress_bar" && cut.progress !== undefined) {
    return maybeWrapWithBg(
      <AbsoluteFill
        style={{
          background: bgColor || theme.surfaceColor,
          display: "flex", alignItems: "center", justifyContent: "center",
          padding: "80px 120px",
        }}
      >
        {cut.title && (
          <div style={{
            position: "absolute", top: 120, fontSize: 48, fontWeight: 700,
            color: textColor, textAlign: "center", width: "100%",
          }}>
            {cut.title}
          </div>
        )}
        <ProgressBar
          progress={cut.progress} label={cut.progressLabel}
          color={cut.progressColor || accent}
          animationStyle={(cut.progressAnimation as any) || "fill"}
          segments={cut.progressSegments} backgroundColor={cut.backgroundColor || theme.surfaceColor}
          animationDurationFrames={durationInFrames}
        />
      </AbsoluteFill>
    );
  }

  // --- Anime scene (multi-image crossfade + particles) ---
  if (cut.type === "anime_scene" && cut.images && cut.images.length > 0) {
    return (
      <AnimeScene
        images={cut.images}
        animation={(cut.animation as CameraMotion) || "ken-burns"}
        particles={cut.particles}
        particleColor={cut.particleColor}
        particleCount={cut.particleCount}
        particleIntensity={cut.particleIntensity}
        backgroundColor={cut.backgroundColor}
        vignette={cut.vignette ?? true}
        lightingFrom={cut.lightingFrom}
        lightingTo={cut.lightingTo}
        sceneDurationSeconds={cut.out_seconds - cut.in_seconds}
      />
    );
  }

  // --- Media types (image / video fallback) ---
  const animation = cut.animation || cut.transform?.animation;

  if (cut.source && isImage(cut.source)) {
    return maybeWrapWithBg(
      <ImageScene src={cut.source} animation={animation} sceneDurationFrames={durationInFrames} />
    );
  }

  if (cut.source && isVideo(cut.source)) {
    return maybeWrapWithBg(
      <VideoScene
        src={cut.source}
        startFrom={cut.source_in_seconds ?? 0}
        transitionIn={cut.transition_in}
        transitionOut={cut.transition_out}
        transitionDuration={cut.transition_duration}
        sceneDurationSeconds={cut.out_seconds - cut.in_seconds}
        backgroundColor={cut.backgroundColor}
      />,
    );
  }

  // Final fallback — try as image if source exists, otherwise show text_card
  if (cut.source) {
    return maybeWrapWithBg(
      <ImageScene src={cut.source} animation={animation} sceneDurationFrames={durationInFrames} />
    );
  }

  // No source, no type — render as text card with cut id as fallback
  return <TextCard text={cut.text || cut.id} color={textColor} backgroundColor={bgColor} sceneDurationFrames={durationInFrames} />;
};

// ---------------------------------------------------------------------------
// Overlay renderer
// ---------------------------------------------------------------------------

const OverlayRenderer: React.FC<{
  overlay: Overlay;
  theme: ThemeConfig;
  /** The overlay Sequence's own frame length — without it the components'
   *  exit choreography would aim at the whole composition (dead code). */
  durationInFrames: number;
}> = ({
  overlay,
  theme,
  durationInFrames,
}) => {
  if (overlay.type === "section_title") {
    return (
      <SectionTitle
        title={overlay.text ?? ""}
        subtitle={overlay.subtitle}
        accentColor={overlay.accentColor || theme.accentColor}
        textColor={theme.textColor}
        position={(overlay.position as any) || "top-left"}
        sceneDurationFrames={durationInFrames}
      />
    );
  }
  if (overlay.type === "stat_reveal") {
    return (
      <StatReveal
        stat={overlay.text ?? ""}
        label={overlay.subtitle}
        accentColor={overlay.accentColor || theme.accentColor}
        textColor={theme.textColor}
        position={(overlay.position as any) || "bottom-right"}
        sceneDurationFrames={durationInFrames}
      />
    );
  }
  if (overlay.type === "hero_title") {
    return (
      <HeroTitle
        title={overlay.text ?? ""}
        subtitle={overlay.subtitle}
        accentColor={overlay.accentColor || theme.accentColor}
        textColor={theme.textColor}
        subtitleColor={theme.mutedTextColor}
        scrimBackground={heroScrim(theme)}
        sceneDurationFrames={durationInFrames}
      />
    );
  }
  if (overlay.type === "provider_chip" && overlay.providers) {
    return (
      <ProviderChip
        providers={overlay.providers as string[]}
        cycleSeconds={overlay.cycleSeconds}
        position={(overlay.position as any) || "bottom-right"}
        accentColor={overlay.accentColor}
        label={overlay.label}
      />
    );
  }
  return null;
};

// ---------------------------------------------------------------------------
// Main composition
// ---------------------------------------------------------------------------

export const Explainer: React.FC<ExplainerProps> = (props) => {
  const { cuts, overlays, captions, audio, sfx } = props;
  const { fps, durationInFrames } = useVideoConfig();

  // Resolve theme from props — playbook name, theme name, or custom themeConfig
  const theme = resolveTheme(props as Record<string, unknown>);

  // Section count for palette spread (falls back to cut count).
  const sectionCount =
    (typeof props.sectionCount === "number" && props.sectionCount > 0)
      ? (props.sectionCount as number)
      : Math.max(1, ...cuts.map((c) => (c.sectionIndex ?? 0) + 1));

  // v2.1: the effective palette ring — custom palettes from the creative
  // director (derived from this project's colors/seed) when provided, else
  // the curated built-ins. Same props file → same ring; different projects
  // → different rings.
  const paletteRing = effectivePalettes(props.customPalettes);

  return (
    <AbsoluteFill style={{ background: theme.backgroundColor, fontFamily: theme.headingFont || fontFamily }}>
      {/* Layer 0: Neutral animated base — visible during scene crossfades */}
      <BaseBackground theme={theme} />

      {/* Layer 1: Visual scenes — each with its own animated background,
          transition choreography, camera drift, and luma sweep */}
      {cuts.map((cut, i) => {
        const from = Math.round(cut.in_seconds * fps);
        const duration = Math.max(1, Math.round((cut.out_seconds - cut.in_seconds) * fps));
        const seed = hashString(cut.id);
        const projectSeed =
          typeof props.creativeSeed === "number" ? (props.creativeSeed as number) : 0;
        const transitionSpeed =
          typeof props.transitionSpeed === "number" ? (props.transitionSpeed as number) : 1;

        // Creative direction: explicit props win, seeded fallbacks fill the
        // gaps so even OLD props files render with full motion variety —
        // and different projects no longer share one modulo sequence.
        const sectionIndex = cut.sectionIndex ?? 0;
        const palette =
          cut.paletteIndex !== undefined
            ? paletteByIndex(cut.paletteIndex)
            : paletteForSectionIn(paletteRing, sectionIndex, sectionCount, i);
        const bgStyle: BgStyle = cut.bgStyle ?? bgStyleForIndex(i + sectionIndex);
        const transition: TransitionStyle =
          cut.transition ?? transitionForCut(i, cut.id, projectSeed);
        const hasBroll = Boolean(cut.backgroundVideo || cut.backgroundImage);

        return (
          <Sequence key={cut.id} from={from} durationInFrames={duration}>
            <CameraDrift durationInFrames={duration} seed={seed}>
              {!hasBroll && (
                <SceneBackground
                  style={bgStyle}
                  palette={palette}
                  seed={seed}
                  durationInFrames={duration}
                />
              )}
              <SceneTransition style={transition} durationInFrames={duration} speed={transitionSpeed}>
                <SceneRenderer
                  cut={cut}
                  theme={theme}
                  palette={palette}
                  durationInFrames={duration}
                />
              </SceneTransition>
            </CameraDrift>
            <TransitionSweep palette={palette} seed={seed} enabled={cut.sweep !== false} />
          </Sequence>
        );
      })}

      {/* Layer 2: Overlays (section titles, stat reveals, hero titles) */}
      {overlays?.map((overlay, i) => {
        const from = Math.round(overlay.in_seconds * fps);
        const duration = Math.max(1, Math.round(
          (overlay.out_seconds - overlay.in_seconds) * fps
        ));

        return (
          <Sequence key={`overlay-${i}`} from={from} durationInFrames={duration}>
            <OverlayRenderer overlay={overlay} theme={theme} durationInFrames={duration} />
          </Sequence>
        );
      })}

      {/* Layer 3b: Channel brand watermark (optional, on top of scenes,
          under captions) — the per-channel identity layer. */}
      {props.brandOverlay && (
        <BrandOverlay brand={props.brandOverlay} durationInFrames={durationInFrames} />
      )}

      {/* Layer 3: Captions (word-by-word highlight, style-pack driven) */}
      {captions && captions.length > 0 && (
        <CaptionOverlay
          words={captions}
          color={theme.textColor}
          highlightColor={theme.captionHighlightColor}
          backgroundColor={theme.captionBackgroundColor}
          captionStyle={props.captionStyle as CaptionStyleConfig | undefined}
        />
      )}

      {/* Layer 4: Audio — narration */}
      {audio?.narration?.src && (
        <Audio src={resolveAsset(audio.narration.src)} volume={audio.narration.volume ?? 1} />
      )}

      {/* Layer 4: Audio — sound-effect cues (whooshes, impacts, chimes).
          v2.1: window length comes from the cue sheet (long risers were
          truncated by the old hardcoded 4s), default stays 4s. */}
      {sfx?.map((cue, i) => (
        <Sequence
          key={`sfx-${i}`}
          from={Math.max(0, Math.round(cue.atSeconds * fps))}
          durationInFrames={Math.round((cue.durationSeconds ?? 4) * fps)}
        >
          <Audio src={resolveAsset(cue.src)} volume={cue.volume ?? 0.6} />
        </Sequence>
      ))}

      {/* Layer 4: Audio — music with offset, fade in/out, and optional loop */}
      {audio?.music?.src && (
        <Audio
          src={resolveAsset(audio.music.src)}
          startFrom={Math.round((audio.music.offsetSeconds ?? 0) * fps)}
          loop={audio.music.loop ?? false}
          loopVolumeCurveBehavior="repeat"
          volume={(f) => {
            const baseVol = audio.music!.volume ?? 0.1;
            const fadeInDur = (audio.music!.fadeInSeconds ?? 2) * fps;
            const fadeOutDur = (audio.music!.fadeOutSeconds ?? 3) * fps;
            const totalFrames = durationInFrames;

            // Fade in
            const fadeIn = interpolate(f, [0, fadeInDur], [0, baseVol], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
            });
            // Fade out
            const fadeOut = interpolate(
              f,
              [totalFrames - fadeOutDur, totalFrames],
              [baseVol, 0],
              { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
            );
            return Math.min(fadeIn, fadeOut);
          }}
        />
      )}
    </AbsoluteFill>
  );
};
