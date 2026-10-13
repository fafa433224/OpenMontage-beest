import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

// ---------------------------------------------------------------------------
// TextCard v2 — kinetic typography.
//
// v1 was a single centered div with one spring scale+fade: fine for one card,
// robotic when EVERY card in the video does the identical move. v2:
//   * word-by-word staggered rise with spring overshoot,
//   * alternating alignment (left / center / right) driven by the `align`
//     prop so consecutive scenes never share a layout,
//   * optional kicker chip (small uppercase label above the text),
//   * 1-3 keywords highlighted in the accent color with a glow halo,
//   * glass panel behind the text (translucent surface + edge),
//   * exit choreography (drop + fade) so cuts leave gracefully too.
//
// The component stays 100% backward compatible: every new prop is optional
// and old props render with `center` + no kicker, looking better than v1
// because of the word stagger and the exit move.
// ---------------------------------------------------------------------------

interface TextCardProps {
  text: string;
  fontSize?: number;
  color?: string;
  backgroundColor?: string;
  /** Layout anchor. The pipeline rotates this so scenes vary. */
  align?: "left" | "center" | "right";
  /** Small uppercase label above the text (e.g. "PART 2 — PIPELINE"). */
  kicker?: string;
  /** Accent color for kicker + highlighted keywords. */
  accentColor?: string;
  /** Words to highlight in the accent color. */
  accentWords?: string[];
  /** Translucent glass panel behind the text (rgba string). */
  glass?: string;
  glassEdge?: string;
  /** Muted color for the kicker. */
  mutedColor?: string;
}

const normalize = (w: string) => w.toLowerCase().replace(/[^a-z0-9]/g, "");

/** Mix a hex color toward white — keyword accents get +30% brightness so
 *  they stay high-contrast over glass panels and busy backgrounds (VLM
 *  feedback: mid-brightness accents can lose legibility). */
function brighten(hex: string, amount = 0.3): string {
  const clean = hex.replace("#", "");
  const full = clean.length === 3 ? clean.split("").map((c) => c + c).join("") : clean;
  const v = parseInt(full, 16);
  const clamp = (x: number) => Math.max(0, Math.min(255, Math.round(x)));
  const r = (v >> 16) & 255, g = (v >> 8) & 255, b = v & 255;
  return `rgb(${clamp(r + (255 - r) * amount)}, ${clamp(g + (255 - g) * amount)}, ${clamp(b + (255 - b) * amount)})`;
}

export const TextCard: React.FC<TextCardProps> = ({
  text,
  fontSize = 62,
  color = "#FFFFFF",
  backgroundColor,
  align = "center",
  kicker,
  accentColor = "#22D3EE",
  accentWords = [],
  glass,
  glassEdge,
  mutedColor,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();

  const words = text.split(/\s+/).filter(Boolean);
  const accentSet = new Set(accentWords.map(normalize).filter(Boolean));

  // Exit choreography — drop slightly and fade over the last 9 frames.
  const outStart = Math.max(20, durationInFrames - 9);
  const exit = interpolate(frame, [outStart, durationInFrames], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: (x) => 1 - Math.pow(1 - x, 2.4),
  });
  const exitY = (1 - exit) * 34;

  // Layout variants — the "anchor" of the block moves per scene.
  const isLeft = align === "left";
  const isRight = align === "right";
  const alignItems = isLeft ? "flex-start" : isRight ? "flex-end" : "center";
  const textAlign = align;
  const blockMax = isLeft || isRight ? "62%" : "78%";

  // Kicker reveal: quick wipe in from the accent side.
  const kickerIn = spring({ frame, fps, config: { damping: 20, stiffness: 120 } });

  const panelStyle: React.CSSProperties = {
    background: glass || "transparent",
    border: glassEdge ? `1px solid ${glassEdge}` : undefined,
    borderRadius: 28,
    boxShadow: glass
      ? "0 24px 80px rgba(0,0,0,0.38), inset 0 1px 0 rgba(255,255,255,0.06)"
      : undefined,
    padding: glass ? "56px 72px" : 0,
    maxWidth: blockMax,
  };

  return (
    <AbsoluteFill
      style={{
        justifyContent: "center",
        alignItems,
        background: backgroundColor && backgroundColor !== "transparent" ? backgroundColor : "transparent",
        padding: "0 120px",
      }}
    >
      <div
        style={{
          ...panelStyle,
          opacity: exit,
          transform: `translateY(${exitY}px)`,
          textAlign,
        }}
      >
        {kicker && (
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 14,
              marginBottom: 26,
              opacity: kickerIn,
              transform: `translateX(${(1 - kickerIn) * (isRight ? 40 : -40)}px)`,
            }}
          >
            <div
              style={{
                width: 34,
                height: 4,
                borderRadius: 2,
                background: accentColor,
              }}
            />
            <span
              style={{
                fontSize: 22,
                fontWeight: 700,
                letterSpacing: "0.22em",
                textTransform: "uppercase",
                color: mutedColor || accentColor,
                fontFamily: "Space Grotesk, Inter, system-ui, sans-serif",
              }}
            >
              {kicker}
            </span>
          </div>
        )}

        <div
          style={{
            fontSize,
            color,
            fontFamily: "Space Grotesk, Inter, system-ui, sans-serif",
            fontWeight: 700,
            lineHeight: 1.28,
            maxWidth: "100%",
          }}
        >
          {words.map((word, i) => {
            // Stagger: each word launches 2.1 frames after the previous one.
            const delay = i * 2.1;
            const wSpring = spring({
              frame: frame - delay,
              fps,
              config: { damping: 14, stiffness: 130, mass: 0.85 },
            });
            const y = interpolate(wSpring, [0, 1], [44, 0]);
            const isAccent = accentSet.has(normalize(word));
            const accentHex = accentColor.startsWith("#") ? accentColor : "#22D3EE";
            return (
              <span
                key={i}
                style={{
                  display: "inline-block",
                  marginRight: "0.28em",
                  opacity: wSpring,
                  transform: `translateY(${y}px)`,
                  color: isAccent ? brighten(accentHex, 0.3) : color,
                  textShadow: isAccent ? `0 0 26px ${accentHex}80` : undefined,
                  willChange: "transform, opacity",
                }}
              >
                {word}
              </span>
            );
          })}
        </div>
      </div>
    </AbsoluteFill>
  );
};
