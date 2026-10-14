import React from "react";
import { Img, staticFile } from "remotion";
import type { BrandOverlayConfig } from "../theme";

/**
 * Channel brand identity layer — the per-channel watermark/wordmark.
 *
 * Rendered on top of every scene when props.brandOverlay is present
 * (emitted by scripts/kaggle/build_explainer_props.py when the run is
 * channel-branded). Pure transform/opacity animation only.
 *
 * Deliberately subtle: a brand watermark that competes with content is
 * worse than none. Opacity defaults to 0.55 for logo, 0.4 for text.
 */

const POSITIONS: Record<string, React.CSSProperties> = {
  "bottom-right": { bottom: 36, right: 48 },
  "bottom-left": { bottom: 36, left: 48 },
  "top-right": { top: 36, right: 48 },
  "top-left": { top: 36, left: 48 },
};

export const BrandOverlay: React.FC<{
  brand: BrandOverlayConfig;
  durationInFrames: number;
}> = ({ brand, durationInFrames }) => {
  const wm = brand.watermark || {};
  if (!wm.enabled) {
    return null;
  }
  const position = wm.position || "bottom-right";
  const opacity = typeof wm.opacity === "number" ? wm.opacity : 0.55;
  const style: React.CSSProperties = {
    position: "absolute",
    ...POSITIONS[position],
    display: "flex",
    alignItems: "center",
    gap: 10,
    opacity,
    zIndex: 90,
    pointerEvents: "none",
    fontFamily: `${brand.fontBody || "Space Grotesk"}, sans-serif`,
  };

  const logo = wm.logo || brand.logo;
  const text = wm.text || brand.name;

  return (
    <div style={style}>
      {logo ? (
        <Img
          src={staticFile(logo)}
          style={{ height: 44, width: "auto", objectFit: "contain" }}
        />
      ) : null}
      {text && !logo ? (
        <span
          style={{
            fontSize: 22,
            fontWeight: 700,
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            color: brand.accent || "#F7F9FF",
            textShadow: "0 2px 12px rgba(0,0,0,0.45)",
          }}
        >
          {text}
        </span>
      ) : null}
    </div>
  );
};
