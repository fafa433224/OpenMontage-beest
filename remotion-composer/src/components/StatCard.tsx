import { AbsoluteFill, spring, useCurrentFrame, useVideoConfig } from "remotion";

// ---------------------------------------------------------------------------
// StatCard v2 — the big-number moment.
//
// v1 floated a bare number + subtitle in open space (VLM feedback: the
// subtitle felt "loose and disconnected"). v2 locks both into one glass
// panel, tightens the coupling (6px), adds a radial glow behind the number,
// a spring overshoot with a landing pulse, and the Space Grotesk system font.
// ---------------------------------------------------------------------------

interface StatCardProps {
  stat: string;
  subtitle?: string;
  statFontSize?: number;
  subtitleFontSize?: number;
  color?: string;
  accentColor?: string;
  backgroundColor?: string;
  /** Translucent glass panel (rgba string) — pass the scene palette glass. */
  glass?: string;
  glassEdge?: string;
}

export const StatCard: React.FC<StatCardProps> = ({
  stat,
  subtitle,
  statFontSize = 132,
  subtitleFontSize = 34,
  color = "#FFFFFF",
  accentColor = "#F59E0B",
  backgroundColor,
  glass,
  glassEdge,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // Landing spring: hard overshoot, then settle.
  const scale = spring({
    frame,
    fps,
    config: { damping: 11, stiffness: 130 },
    from: 0.72,
    to: 1,
  });

  const subtitleOpacity = spring({
    frame: frame - 6,
    fps,
    config: { damping: 20 },
  });

  // Breathing glow behind the number.
  const glowPulse = 0.75 + 0.25 * Math.sin(frame * 0.06);

  return (
    <AbsoluteFill
      style={{
        justifyContent: "center",
        alignItems: "center",
        background:
          backgroundColor && backgroundColor !== "transparent" ? backgroundColor : "transparent",
      }}
    >
      <div
        style={{
          textAlign: "center",
          maxWidth: 1480,
          padding: glass ? "48px 88px" : "0 80px",
          borderRadius: 32,
          background: glass || "transparent",
          border: glassEdge ? `1px solid ${glassEdge}` : undefined,
          boxShadow: glass
            ? "0 30px 90px rgba(0,0,0,0.42), inset 0 1px 0 rgba(255,255,255,0.06)"
            : undefined,
        }}
      >
        {/* width:100% + maxWidth forces wrapping. Without it the flex item
            shrink-to-fit sized the subtitle to its single-line max-content
            width, and the text ran off the right edge of the canvas (observed
            in a 712s reference render: a 140-char subtitle clipped mid-word
            at 1920px). */}
        <div style={{ position: "relative", width: "100%" }}>
          {/* Radial glow behind the number */}
          <div
            style={{
              position: "absolute",
              left: "50%",
              top: "42%",
              width: 900,
              height: 420,
              transform: "translate(-50%, -50%)",
              borderRadius: "50%",
              background: `radial-gradient(ellipse at center, ${accentColor}30 0%, transparent 65%)`,
              opacity: glowPulse,
            }}
          />
          <div
            style={{
              position: "relative",
              transform: `scale(${scale})`,
              fontSize: statFontSize,
              color: accentColor,
              fontFamily: "Space Grotesk, Inter, system-ui, sans-serif",
              fontWeight: 800,
              lineHeight: 1.05,
              overflowWrap: "break-word",
              textShadow: `0 0 60px ${accentColor}44`,
            }}
          >
            {stat}
          </div>
        </div>
        {subtitle && (
          <div
            style={{
              opacity: subtitleOpacity,
              fontSize: subtitleFontSize,
              color,
              fontFamily: "Space Grotesk, Inter, system-ui, sans-serif",
              fontWeight: 400,
              marginTop: 6,
              width: "100%",
              lineHeight: 1.4,
              overflowWrap: "break-word",
            }}
          >
            {subtitle}
          </div>
        )}
      </div>
    </AbsoluteFill>
  );
};
