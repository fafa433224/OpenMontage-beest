import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

type ProgressAnimationStyle = "fill" | "pulse" | "step";

interface ProgressSegment {
  value: number;
  color?: string;
  label?: string;
}

interface ProgressBarProps {
  /** Target fill: 0-100 percent, or a 0-1 fraction (auto-normalised).
   *  Callers in the edit-decisions world pass fractions (0.4 for 40%),
   *  so any value <= 1 is treated as a fraction and scaled to percent. */
  progress: number;
  label?: string;
  color?: string;
  backgroundColor?: string;
  trackColor?: string;
  showPercentage?: boolean;
  animationStyle?: ProgressAnimationStyle;
  segments?: ProgressSegment[];
  height?: number;
  borderRadius?: number;
  fontFamily?: string;
  textColor?: string;
  labelFontSize?: number;
  percentageFontSize?: number;
  /** Frame window for the fill animation. When the bar is embedded in a
   *  Remotion <Sequence> (e.g. an Explainer cut), pass the cut's own frame
   * duration — useVideoConfig().durationInFrames there still reports the
   * WHOLE composition, which stretches the fill over the entire video and
   * leaves the bar visibly stuck near zero inside a short cut. */
  animationDurationFrames?: number;
}

export const ProgressBar: React.FC<ProgressBarProps> = ({
  progress,
  label,
  color = "#2563EB",
  backgroundColor = "#FFFFFF",
  trackColor = "#E5E7EB",
  showPercentage = true,
  animationStyle = "fill",
  segments,
  height = 32,
  borderRadius = 8,
  fontFamily = "Inter, system-ui, sans-serif",
  textColor = "#1F2937",
  labelFontSize = 36,
  percentageFontSize = 28,
  animationDurationFrames,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();

  // Normalise the two calling conventions onto one 0-100 percent scale.
  // A value <= 1 means "fraction of the whole" (0.4 -> 40%); values above 1
  // are already percent. A deliberate 1% bar is effectively never wanted,
  // while fraction props are the norm in the pipeline's cut payloads.
  const normalizedProgress = progress <= 1 ? progress * 100 : progress;
  const clampedProgress = Math.max(0, Math.min(100, normalizedProgress));

  // The fill animation must play out within this bar's own on-screen window.
  // Embedded in a Sequence, useVideoConfig().durationInFrames is the whole
  // composition — pass animationDurationFrames (the cut's frame length) so a
  // 20-second progress cut actually fills its bar instead of creeping.
  const animationFrames = animationDurationFrames ?? durationInFrames;

  // Container entrance animation
  const containerOpacity = spring({
    frame,
    fps,
    config: { damping: 20 },
  });
  const containerScale = spring({
    frame,
    fps,
    config: { damping: 15, stiffness: 100 },
    from: 0.95,
    to: 1,
  });

  // Build fill width based on animation style
  let fillFraction: number;

  if (animationStyle === "fill") {
    // Fill settles by 40% of the cut — mid-scene census/QC frames then show
    // the final value (label and counter always agree once settled; a 50%
    // window left the count-up's last rounding frame exposed at midpoints).
    fillFraction = interpolate(
      frame,
      [10, Math.max(30, animationFrames * 0.4)],
      [0, clampedProgress / 100],
      { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
    );
  } else if (animationStyle === "pulse") {
    const base = interpolate(
      frame,
      [10, Math.max(30, animationFrames * 0.4)],
      [0, clampedProgress / 100],
      { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
    );
    // Subtle pulse once fill completes
    const pulsePhase = interpolate(
      frame,
      [animationFrames * 0.4, animationFrames * 0.9],
      [0, Math.PI * 4],
      { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
    );
    const pulseScale =
      base >= clampedProgress / 100 - 0.01
        ? 1 + Math.sin(pulsePhase) * 0.015
        : 1;
    fillFraction = base * pulseScale;
  } else {
    // step — discrete jumps
    const stepCount = segments ? segments.length : 5;
    const rawProgress = interpolate(
      frame,
      [10, Math.max(30, animationFrames * 0.6)],
      [0, clampedProgress / 100],
      { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
    );
    fillFraction =
      Math.floor(rawProgress * stepCount) / stepCount;
  }

  // Percentage label spring (appears after fill starts)
  const percentOpacity = spring({
    frame: frame - 15,
    fps,
    config: { damping: 20 },
  });

  // Rendered percentage tracks fill
  const displayedPercent = Math.round(fillFraction * 100);

  // Segmented variant
  const isSegmented = segments && segments.length > 0;

  // Bar track dimensions (centered, 70% canvas width)
  const trackWidth = 1344; // 70% of 1920
  const trackLeft = (1920 - trackWidth) / 2;
  const trackTop = label ? 520 : 500;

  return (
    <AbsoluteFill
      style={{
        background: backgroundColor,
        justifyContent: "center",
        alignItems: "center",
      }}
    >
      <div
        style={{
          opacity: containerOpacity,
          transform: `scale(${containerScale})`,
          width: "100%",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 24,
        }}
      >
        {/* Label */}
        {label && (
          <div
            style={{
              fontFamily,
              fontWeight: 700,
              fontSize: labelFontSize,
              color: textColor,
              textAlign: "center",
              opacity: spring({
                frame,
                fps,
                config: { damping: 20 },
              }),
            }}
          >
            {label}
          </div>
        )}

        {/* Track */}
        <div
          style={{
            width: `${(trackWidth / 1920) * 100}%`,
            position: "relative",
          }}
        >
          <div
            style={{
              width: "100%",
              height,
              backgroundColor: trackColor,
              borderRadius,
              overflow: "hidden",
              position: "relative",
              display: "flex",
            }}
          >
            {isSegmented ? (
              // Segmented bars
              segments!.map((seg, i) => {
                const segDelay = 10 + i * 8;
                const segProgress = spring({
                  frame: frame - segDelay,
                  fps,
                  config: { damping: 14, stiffness: 80 },
                });
                const segWidth = (seg.value / 100) * 100;
                return (
                  <div
                    key={i}
                    style={{
                      width: `${segWidth}%`,
                      height: "100%",
                      backgroundColor: seg.color || color,
                      transform: `scaleX(${segProgress})`,
                      transformOrigin: "left",
                      borderRight:
                        i < segments!.length - 1
                          ? `2px solid ${backgroundColor}`
                          : "none",
                    }}
                  />
                );
              })
            ) : (
              // Single fill bar
              <div
                style={{
                  width: `${fillFraction * 100}%`,
                  height: "100%",
                  backgroundColor: color,
                  borderRadius,
                  transition:
                    animationStyle === "step"
                      ? "width 0.15s ease"
                      : undefined,
                }}
              />
            )}
          </div>

          {/* Segment labels below track */}
          {isSegmented && (
            <div
              style={{
                display: "flex",
                width: "100%",
                marginTop: 8,
              }}
            >
              {segments!.map((seg, i) => {
                const segDelay = 10 + i * 8;
                const labelOp = spring({
                  frame: frame - segDelay - 6,
                  fps,
                  config: { damping: 20 },
                });
                return (
                  <div
                    key={i}
                    style={{
                      width: `${(seg.value / 100) * 100}%`,
                      fontFamily,
                      fontWeight: 500,
                      fontSize: 18,
                      color: textColor,
                      textAlign: "center",
                      opacity: labelOp,
                    }}
                  >
                    {seg.label}
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Percentage */}
        {showPercentage && !isSegmented && (
          <div
            style={{
              fontFamily,
              fontWeight: 800,
              fontSize: percentageFontSize,
              color,
              opacity: percentOpacity,
            }}
          >
            {displayedPercent}%
          </div>
        )}
      </div>
    </AbsoluteFill>
  );
};
