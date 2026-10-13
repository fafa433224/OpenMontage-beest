import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

// ---------------------------------------------------------------------------
// HeroTitle v2 — keeps the per-character spring stagger that worked, and adds
// the missing cinematic layers: a gradient shine that sweeps across the title
// right after it lands, an accent bar that snaps in from the left, a camera
// punch-in on the whole block, and an exit fade/lift so heroes hand off to
// the next scene gracefully instead of hard-cutting.
// ---------------------------------------------------------------------------

type HeroTitleProps = {
  title: string;
  subtitle?: string;
  /** Color of the leading accent characters and the underline. */
  accentColor?: string;
  /** Color of the remaining title characters. Pass the theme's textColor. */
  textColor?: string;
  /** Subtitle color. */
  subtitleColor?: string;
  /**
   * Scrim painted behind the title so it separates from whatever is underneath.
   * Pass "none" to skip the scrim entirely (per-scene backgrounds are rich
   * enough on their own and deserve to be seen).
   */
  scrimBackground?: string;
  /** Skip the dark wash — new per-scene backgrounds don't need it. */
  noScrim?: boolean;
};

const DEFAULT_SCRIM =
  "radial-gradient(ellipse at center, rgba(15,23,42,0.35) 0%, rgba(15,23,42,0.55) 100%)";

export const HeroTitle: React.FC<HeroTitleProps> = ({
  title,
  subtitle,
  accentColor = "#22D3EE",
  textColor = "#F8FAFC",
  subtitleColor = "#A78BFA",
  scrimBackground = DEFAULT_SCRIM,
  noScrim = false,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();

  const titleChars = title.split("");

  // Camera punch: whole block settles from 1.06 → 1.0.
  const punch = spring({ frame, fps, config: { damping: 17, stiffness: 90, mass: 1.1 } });
  const blockScale = interpolate(punch, [0, 1], [1.06, 1.0]);

  // Exit: lift + fade over the last 10 frames.
  const outStart = Math.max(20, durationInFrames - 10);
  const exit = interpolate(frame, [outStart, durationInFrames], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: (x) => 1 - Math.pow(1 - x, 2.2),
  });
  const exitY = (1 - exit) * -36;

  // Shine sweep across the title — a masked gradient band whose position is
  // animated with background-position (cheap: repaint, no filter).
  const shineDelay = titleChars.length * 1.2 + 8;
  const shineP = interpolate(frame, [shineDelay, shineDelay + 16], [-60, 160], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const scrim = noScrim || scrimBackground === "none" ? "transparent" : scrimBackground;

  return (
    <AbsoluteFill
      style={{
        justifyContent: "center",
        alignItems: "center",
        background: scrim,
      }}
    >
      <div
        style={{
          textAlign: "center",
          maxWidth: "85%",
          opacity: exit,
          transform: `scale(${blockScale}) translateY(${exitY}px)`,
        }}
      >
        {/* Accent bar — snaps in from the left before the letters land */}
        <div
          style={{
            margin: "0 auto 26px",
            height: 5,
            borderRadius: 3,
            backgroundColor: accentColor,
            width: interpolate(
              spring({ frame: frame - 2, fps, config: { damping: 15, stiffness: 120 } }),
              [0, 1],
              [0, 140],
            ),
            boxShadow: `0 0 18px ${accentColor}55`,
          }}
        />

        {/* Main title with per-character spring */}
        <div
          style={{
            fontSize: 76,
            fontWeight: 800,
            fontFamily: "Space Grotesk, Inter, system-ui, sans-serif",
            lineHeight: 1.18,
            display: "flex",
            justifyContent: "center",
            flexWrap: "wrap",
            gap: 0,
          }}
        >
          {titleChars.map((char, i) => {
            const delay = i * 1.2;
            const charSpring = spring({
              frame: frame - delay,
              fps,
              config: { damping: 12, stiffness: 150 },
            });

            return (
              <span
                key={i}
                style={{
                  display: "inline-block",
                  opacity: charSpring,
                  transform: `translateY(${interpolate(charSpring, [0, 1], [34, 0])}px)`,
                  color: i < 8 ? accentColor : textColor,
                  whiteSpace: char === " " ? "pre" : undefined,
                  minWidth: char === " " ? "0.3em" : undefined,
                  // Shine: characters inside the band window brighten.
                  textShadow:
                    Math.abs(i * 1.9 - shineP) < 26
                      ? `0 0 22px ${textColor}aa, 0 0 44px ${accentColor}66`
                      : undefined,
                }}
              >
                {char}
              </span>
            );
          })}
        </div>

        {/* Subtitle */}
        {subtitle && (
          <div
            style={{
              marginTop: 22,
              opacity: spring({
                frame: frame - titleChars.length * 1.2 - 5,
                fps,
                config: { damping: 20 },
              }),
              fontSize: 27,
              fontWeight: 400,
              color: subtitleColor,
              fontFamily: "Space Grotesk, Inter, system-ui, sans-serif",
              letterSpacing: "0.14em",
              textTransform: "uppercase",
            }}
          >
            {subtitle}
          </div>
        )}

        {/* Animated underline */}
        <div
          style={{
            margin: "26px auto 0",
            height: 3,
            backgroundColor: accentColor,
            borderRadius: 2,
            width: interpolate(
              spring({
                frame: frame - 15,
                fps,
                config: { damping: 15, stiffness: 60 },
              }),
              [0, 1],
              [0, 420],
            ),
            boxShadow: `0 0 14px ${accentColor}44`,
          }}
        />
      </div>
    </AbsoluteFill>
  );
};
