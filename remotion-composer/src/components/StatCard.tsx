import { AbsoluteFill, spring, useCurrentFrame, useVideoConfig } from "remotion";

interface StatCardProps {
  stat: string;
  subtitle?: string;
  statFontSize?: number;
  subtitleFontSize?: number;
  color?: string;
  accentColor?: string;
  backgroundColor?: string;
}

export const StatCard: React.FC<StatCardProps> = ({
  stat,
  subtitle,
  statFontSize = 128,
  subtitleFontSize = 36,
  color = "#FFFFFF",
  accentColor = "#F59E0B",
  backgroundColor = "#1F2937",
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const scale = spring({
    frame,
    fps,
    config: { damping: 12, stiffness: 120 },
    from: 0.8,
    to: 1,
  });

  const subtitleOpacity = spring({
    frame: frame - 8,
    fps,
    config: { damping: 20 },
  });

  return (
    <AbsoluteFill
      style={{
        justifyContent: "center",
        alignItems: "center",
        background: backgroundColor,
      }}
    >
      {/* width:100% + maxWidth forces wrapping. Without it the flex item
          shrink-to-fit sized the subtitle to its single-line max-content
          width, and the text ran off the right edge of the canvas (observed
          in a 712s reference render: a 140-char subtitle clipped mid-word
          at 1920px). */}
      <div style={{ textAlign: "center", width: "100%", maxWidth: 1440, padding: "0 80px" }}>
        <div
          style={{
            transform: `scale(${scale})`,
            fontSize: statFontSize,
            color: accentColor,
            fontFamily: "Inter, system-ui, sans-serif",
            fontWeight: 800,
            lineHeight: 1.1,
            overflowWrap: "break-word",
          }}
        >
          {stat}
        </div>
        {subtitle && (
          <div
            style={{
              opacity: subtitleOpacity,
              fontSize: subtitleFontSize,
              color,
              fontFamily: "Inter, system-ui, sans-serif",
              fontWeight: 400,
              marginTop: 16,
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
