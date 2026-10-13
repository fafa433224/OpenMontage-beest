// ---------------------------------------------------------------------------
// Scene palette system — every section of the video gets its own color world.
//
// The single biggest complaint about v1 was that ONE background (same colors,
// same orbs, same grid) ran under the entire video and never changed. This
// module gives each *section* a distinct palette and each *cut* a distinct
// background style, so the visual world evolves as the narration moves on.
//
// Design rules:
//  * All palettes keep light text readable (dark bases, saturated glows).
//  * Palettes are curated, not generated, so every combination has been
//    eyeballed for taste: deep indigo → teal → amber → emerald → magenta
//    → steel. Six is enough for a 2-6 section video to never repeat.
//  * Deterministic: same section index always yields the same palette.
// ---------------------------------------------------------------------------

export interface ScenePalette {
  name: string;
  /** Deep base color behind everything in the section. */
  bgBase: string;
  /** Secondary wash blended into the base gradient. */
  bgAlt: string;
  /** Primary glow color (background blobs / beams / waves). */
  glowA: string;
  /** Secondary glow color. */
  glowB: string;
  /** Accent for highlights, bars, sweeps, keyword text. */
  accent: string;
  /** Main text color. */
  text: string;
  /** Muted text color (subtitles, kickers). */
  muted: string;
  /** Translucent glass surface for cards (rgba). */
  glass: string;
  /** Brighter glass edge for card borders (rgba). */
  glassEdge: string;
}

export const SECTION_PALETTES: ScenePalette[] = [
  {
    name: "deep-indigo",
    bgBase: "#0B1026",
    bgAlt: "#151B3B",
    glowA: "#6D5BF6",
    glowB: "#2DD4BF",
    accent: "#8B7CFF",
    text: "#F4F6FF",
    muted: "#9BA3CF",
    glass: "rgba(21, 27, 59, 0.72)",
    glassEdge: "rgba(139, 124, 255, 0.35)",
  },
  {
    name: "teal-depth",
    bgBase: "#061821",
    bgAlt: "#0B2E3A",
    glowA: "#10B9A9",
    glowB: "#3E8ED0",
    accent: "#2DD4BF",
    text: "#EAFDFB",
    muted: "#8FB8B3",
    glass: "rgba(11, 46, 58, 0.72)",
    glassEdge: "rgba(45, 212, 191, 0.35)",
  },
  {
    name: "amber-dusk",
    bgBase: "#1D1106",
    bgAlt: "#33200C",
    glowA: "#F59E0B",
    glowB: "#EF4444",
    accent: "#FBBF24",
    text: "#FFF9EE",
    muted: "#C4A882",
    glass: "rgba(51, 32, 12, 0.72)",
    glassEdge: "rgba(251, 191, 36, 0.35)",
  },
  {
    name: "emerald-night",
    bgBase: "#05170F",
    bgAlt: "#0C2E1F",
    glowA: "#10B981",
    glowB: "#84CC16",
    accent: "#34D399",
    text: "#F0FFF6",
    muted: "#93BFA6",
    glass: "rgba(12, 46, 31, 0.72)",
    glassEdge: "rgba(52, 211, 153, 0.35)",
  },
  {
    name: "magenta-neon",
    bgBase: "#19081F",
    bgAlt: "#2A0F33",
    glowA: "#EC4899",
    glowB: "#8B5CF6",
    accent: "#F472B6",
    text: "#FDF4FF",
    muted: "#C49BD1",
    glass: "rgba(42, 15, 51, 0.72)",
    glassEdge: "rgba(244, 114, 182, 0.35)",
  },
  {
    name: "steel-focus",
    bgBase: "#0D1218",
    bgAlt: "#1B2733",
    glowA: "#38BDF8",
    glowB: "#A78BFA",
    accent: "#38BDF8",
    text: "#F2F8FF",
    muted: "#9AAEC2",
    glass: "rgba(27, 39, 51, 0.72)",
    glassEdge: "rgba(56, 189, 248, 0.35)",
  },
];

export function paletteForSection(
  sectionIndex: number,
  sectionCount: number,
  offset = 0,
): ScenePalette {
  const n = SECTION_PALETTES.length;
  if (sectionCount <= 1) return SECTION_PALETTES[offset % n];
  // Spread sections across the palette ring with a step that avoids neighbors
  // repeating when sectionCount shares a factor with the ring size.
  const step = Math.max(1, Math.floor(n / Math.min(sectionCount, n)));
  return SECTION_PALETTES[(offset + sectionIndex * step) % n];
}

export function paletteByIndex(index: number): ScenePalette {
  return SECTION_PALETTES[((index % SECTION_PALETTES.length) + SECTION_PALETTES.length) % SECTION_PALETTES.length];
}

// Deterministic PRNG (mulberry32) — backgrounds must render identically on
// every machine and every retry, so no Math.random() anywhere in motion code.
export function seededRandom(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function hashString(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}
