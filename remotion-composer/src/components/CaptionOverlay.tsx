import React, { Fragment } from "react";
import {
  AbsoluteFill,
  Sequence,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

// ---------------------------------------------------------------------------
// CaptionOverlay — word-level captions with STYLE PACKS.
//
// v2.1: the single hardcoded look (glass pill, cyan highlight, bottom) was
// exactly why every video's captions looked identical. The component now
// supports six complete style packs, chosen per project by the creative
// director (lib/creative_director.py) and threaded through Explainer props
// as `captionStyle`. Old props files (no captionStyle) keep rendering with
// the classic pill look — full backward compatibility.
//
// Performance contract: transforms + opacity only, no filter:blur(), no CSS
// transitions. The karaoke-bar wipe uses a width transform, not clip-path
// animation.
// ---------------------------------------------------------------------------

// Word-level caption for TikTok-style highlight display
export interface WordCaption {
  word: string;
  startMs: number;
  endMs: number;
  // Force a page break after this word (e.g. sentence or scene boundaries).
  // Useful for CJK captions where pages should align with clause boundaries.
  pageBreakAfter?: boolean;
}

export type CaptionLook =
  | "pill" // rounded glass pill, glow-highlighted active word (classic)
  | "boxed" // solid high-contrast box, editorial
  | "outline" // no container, heavy text stroke, music-video
  | "karaoke-bar" // full-width bottom bar, wipe-fill highlight
  | "pop-scale" // active word pops in scale, max energy
  | "lower-third"; // documentary lower-third, left aligned

export type CaptionAnimation = "spring" | "slide" | "fade" | "pop";
export type CaptionPosition = "bottom" | "center" | "bottom-bar" | "lower-third";

export interface CaptionStyleConfig {
  look?: CaptionLook;
  wordsPerPage?: number;
  fontSize?: number;
  animation?: CaptionAnimation;
  position?: CaptionPosition;
  color?: string;
  highlightColor?: string;
  backgroundColor?: string;
  fontFamily?: string;
  /** Extra scale multiplier (e.g. 1.15 for vertical renders). */
  scale?: number;
}

type CaptionOverlayProps = {
  words: WordCaption[];
  // How many words to show at once in a "page"
  wordsPerPage?: number;
  fontSize?: number;
  color?: string;
  highlightColor?: string;
  backgroundColor?: string;
  fontFamily?: string;
  // Separator rendered between words. Space-delimited languages want the
  // default " "; CJK languages (no inter-word spacing) should pass "".
  wordSeparator?: string;
  /** Full style-pack config from the creative director. */
  captionStyle?: CaptionStyleConfig;
};

interface CaptionPage {
  words: WordCaption[];
  startMs: number;
  endMs: number;
}

function buildPages(words: WordCaption[], wordsPerPage: number): CaptionPage[] {
  const pages: CaptionPage[] = [];
  let pageWords: WordCaption[] = [];
  const flush = () => {
    if (pageWords.length === 0) return;
    pages.push({
      words: pageWords,
      startMs: pageWords[0].startMs,
      endMs: pageWords[pageWords.length - 1].endMs,
    });
    pageWords = [];
  };
  for (const w of words) {
    pageWords.push(w);
    if (pageWords.length >= wordsPerPage || w.pageBreakAfter) flush();
  }
  flush();
  return pages;
}

/** Frame-entrance value per animation variant: 0 → 1 progress. */
function useEntrance(animation: CaptionAnimation): number {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  switch (animation) {
    case "slide":
      return interpolate(frame, [0, 7], [0, 1], {
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
        easing: (x) => 1 - Math.pow(1 - x, 3),
      });
    case "fade":
      return interpolate(frame, [0, 6], [0, 1], {
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
      });
    case "pop": {
      const s = spring({ frame, fps, config: { damping: 10, stiffness: 200, mass: 0.6 } });
      return s;
    }
    case "spring":
    default:
      return spring({ frame, fps, config: { damping: 18, stiffness: 120 } });
  }
}

const PageRenderer: React.FC<{
  page: CaptionPage;
  fontSize: number;
  color: string;
  highlightColor: string;
  backgroundColor: string;
  fontFamily: string;
  wordSeparator: string;
  look: CaptionLook;
  animation: CaptionAnimation;
  position: CaptionPosition;
  scale: number;
}> = ({
  page,
  fontSize,
  color,
  highlightColor,
  backgroundColor,
  fontFamily,
  wordSeparator,
  look,
  animation,
  position,
  scale,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const entrance = useEntrance(animation);

  const currentMs = page.startMs + (frame / fps) * 1000;

  // Positioning.
  const positionStyle: React.CSSProperties =
    position === "center"
      ? { justifyContent: "center", alignItems: "center" }
      : position === "bottom-bar"
        ? { justifyContent: "flex-end", alignItems: "stretch" }
        : position === "lower-third"
          ? { justifyContent: "flex-end", alignItems: "flex-start", paddingBottom: 90, paddingLeft: 70 }
          : { justifyContent: "flex-end", alignItems: "center", paddingBottom: 80 };

  // Entrance transform per pack.
  const enterTransform =
    animation === "slide"
      ? `translateY(${(1 - entrance) * 26}px)`
      : animation === "pop"
        ? `scale(${0.8 + entrance * 0.2})`
        : `translateY(${interpolate(entrance, [0, 1], [20, 0])}px)`;

  // Fraction of the page elapsed — drives the karaoke wipe.
  const pageElapsed = Math.min(
    1,
    Math.max(0, (currentMs - page.startMs) / Math.max(1, page.endMs - page.startMs)),
  );

  // ---- Container look per pack --------------------------------------------
  let containerStyle: React.CSSProperties;
  switch (look) {
    case "boxed":
      containerStyle = {
        backgroundColor,
        borderRadius: 4,
        padding: "12px 22px",
        maxWidth: "86%",
        textAlign: "center",
        boxShadow: "0 6px 24px rgba(0,0,0,0.45)",
      };
      break;
    case "outline":
      containerStyle = {
        padding: "6px 10px",
        maxWidth: "90%",
        textAlign: "center",
      };
      break;
    case "karaoke-bar":
      containerStyle = {
        width: "100%",
        backgroundColor,
        padding: "14px 0",
        textAlign: "center",
      };
      break;
    case "pop-scale":
      containerStyle = {
        backgroundColor: "transparent",
        padding: "8px 14px",
        maxWidth: "92%",
        textAlign: "center",
      };
      break;
    case "lower-third":
      containerStyle = {
        backgroundColor,
        borderLeft: `6px solid ${highlightColor}`,
        borderRadius: 4,
        padding: "12px 24px",
        maxWidth: "72%",
        textAlign: "left",
      };
      break;
    case "pill":
    default:
      containerStyle = {
        backgroundColor,
        borderRadius: 12,
        padding: "14px 28px",
        maxWidth: "80%",
        textAlign: "center",
      };
      break;
  }

  return (
    <AbsoluteFill style={positionStyle}>
      <div
        style={{
          opacity: entrance,
          transform: enterTransform,
          ...containerStyle,
        }}
      >
        {look === "karaoke-bar" ? (
          // Karaoke: one full-width bar; a highlight layer is revealed
          // left→right with playback progress via width transform.
          <div style={{ position: "relative", display: "inline-block" }}>
            <span
              style={{
                fontSize: fontSize * scale,
                fontWeight: 800,
                fontFamily,
                lineHeight: 1.35,
                color,
                whiteSpace: "pre-wrap",
              }}
            >
              {page.words.map((w, i) => (
                <Fragment key={`${w.startMs}-${i}`}>
                  <span style={{ display: "inline-block", whiteSpace: "nowrap" }}>
                    {w.word}
                  </span>
                  {i < page.words.length - 1 ? wordSeparator : ""}
                </Fragment>
              ))}
            </span>
            <span
              style={{
                position: "absolute",
                inset: 0,
                display: "inline-block",
                overflow: "hidden",
                width: `${pageElapsed * 100}%`,
                verticalAlign: "top",
              }}
            >
              <span
                style={{
                  fontSize: fontSize * scale,
                  fontWeight: 800,
                  fontFamily,
                  lineHeight: 1.35,
                  color: highlightColor,
                  whiteSpace: "pre-wrap",
                  display: "inline-block",
                  width: `${100 / Math.max(0.01, pageElapsed)}%`,
                }}
              >
                {page.words.map((w, i) => (
                  <Fragment key={`${w.startMs}-${i}`}>
                    <span style={{ display: "inline-block", whiteSpace: "nowrap" }}>
                      {w.word}
                    </span>
                    {i < page.words.length - 1 ? wordSeparator : ""}
                  </Fragment>
                ))}
              </span>
            </span>
          </div>
        ) : (
          <span
            style={{
              fontSize: fontSize * scale,
              fontWeight: look === "outline" || look === "pop-scale" ? 900 : 700,
              fontFamily,
              lineHeight: 1.4,
              whiteSpace: "pre-wrap",
              ...(look === "outline"
                ? {
                    // Heavy stroke via layered text-shadow (no webkit-text-stroke
                    // rasterisation variance).
                    textShadow: `-3px -3px 0 #000, 3px -3px 0 #000, -3px 3px 0 #000, 3px 3px 0 #000, 0 6px 14px rgba(0,0,0,0.6)`,
                  }
                : {}),
            }}
          >
            {page.words.map((w, i) => {
              const isActive = w.startMs <= currentMs && w.endMs > currentMs;
              const isPast = w.endMs <= currentMs;
              const activeScale =
                look === "pop-scale" && isActive
                  ? 1 + 0.16 * Math.sin(Math.min(1, (currentMs - w.startMs) / 140) * Math.PI)
                  : 1;
              return (
                <Fragment key={`${w.startMs}-${i}`}>
                  <span
                    style={{
                      display: "inline-block",
                      whiteSpace: "nowrap",
                      transform: activeScale !== 1 ? `scale(${activeScale})` : undefined,
                      color: isActive
                        ? highlightColor
                        : isPast
                          ? color
                          : look === "outline"
                            ? `${color}DD`
                            : `${color}99`,
                      transition: "none", // CSS transitions forbidden in Remotion
                      textShadow: isActive
                        ? look === "outline"
                          ? `-3px -3px 0 #000, 3px -3px 0 #000, -3px 3px 0 #000, 3px 3px 0 #000, 0 0 26px ${highlightColor}88`
                          : `0 0 20px ${highlightColor}66, 0 2px 4px rgba(0,0,0,0.5)`
                        : look === "outline"
                          ? `-3px -3px 0 #000, 3px -3px 0 #000, -3px 3px 0 #000, 3px 3px 0 #000`
                          : "0 2px 4px rgba(0,0,0,0.5)",
                    }}
                  >
                    {w.word}
                  </span>
                  {i < page.words.length - 1 ? wordSeparator : ""}
                </Fragment>
              );
            })}
          </span>
        )}
      </div>
    </AbsoluteFill>
  );
};

export const CaptionOverlay: React.FC<CaptionOverlayProps> = ({
  words,
  wordsPerPage,
  fontSize,
  color,
  highlightColor,
  backgroundColor,
  fontFamily,
  wordSeparator = " ",
  captionStyle,
}) => {
  const { fps } = useVideoConfig();

  // Style-pack resolution: explicit prop > captionStyle > classic default.
  const look: CaptionLook = captionStyle?.look ?? "pill";
  const effectiveWordsPerPage = wordsPerPage ?? captionStyle?.wordsPerPage ?? 6;
  const effectiveFontSize = fontSize ?? captionStyle?.fontSize ?? 42;
  const animation = captionStyle?.animation ?? "spring";
  const position = captionStyle?.position ?? "bottom";
  const scale = captionStyle?.scale ?? 1;

  const effectiveColor = color ?? "#F8FAFC";
  const effectiveHighlight = highlightColor ?? captionStyle?.highlightColor ?? "#22D3EE";
  const effectiveBackground =
    backgroundColor ??
    captionStyle?.backgroundColor ??
    (look === "boxed"
      ? "rgba(8, 11, 20, 0.92)"
      : look === "karaoke-bar"
        ? "rgba(8, 11, 20, 0.85)"
        : look === "lower-third"
          ? "rgba(10, 14, 24, 0.88)"
          : look === "outline" || look === "pop-scale"
            ? "transparent"
            : "rgba(15, 23, 42, 0.75)");
  const effectiveFont =
    fontFamily ??
    captionStyle?.fontFamily ??
    (look === "lower-third"
      ? "Inter, Space Grotesk, system-ui, sans-serif"
      : "Space Grotesk, Inter, system-ui, sans-serif");

  const pages = buildPages(words, effectiveWordsPerPage);

  return (
    <AbsoluteFill>
      {pages.map((page, i) => {
        const fromFrame = Math.round((page.startMs / 1000) * fps);
        const nextStart = pages[i + 1]?.startMs ?? page.endMs + 500;
        const duration = Math.max(
          1,
          Math.round(((nextStart - page.startMs) / 1000) * fps)
        );

        return (
          <Sequence key={i} from={fromFrame} durationInFrames={duration}>
            <PageRenderer
              page={page}
              fontSize={effectiveFontSize}
              color={effectiveColor}
              highlightColor={effectiveHighlight}
              backgroundColor={effectiveBackground}
              fontFamily={effectiveFont}
              wordSeparator={wordSeparator}
              look={look}
              animation={animation}
              position={position}
              scale={scale}
            />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
