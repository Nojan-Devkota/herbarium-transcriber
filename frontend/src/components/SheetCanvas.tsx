import type { Region } from "../api";
import { api } from "../api";

export const KIND_COLORS: Record<Region["kind"], string> = {
  label: "#2f7d4f",
  slip: "#8a4fbf",
  barcode: "#2b6cb0",
  stamp: "#b5651d",
  ruler: "#6b7280",
  color_chart: "#6b7280",
  sheet: "#374151",
};

const KIND_NAMES: Record<Region["kind"], string> = {
  label: "label",
  slip: "slip",
  barcode: "barcode",
  stamp: "stamp",
  ruler: "ruler",
  color_chart: "colour chart",
  sheet: "whole sheet",
};

interface Props {
  sheetId: number;
  width: number;
  height: number;
  regions: Region[];
  selectedRegionId: number | null;
  onSelectRegion?: (id: number) => void;
}

/** The sheet image with SAM 3.1 region overlays drawn in full-resolution sheet coordinates. */
export default function SheetCanvas({ sheetId, width, height, regions, selectedRegionId, onSelectRegion }: Props) {
  const font = Math.max(width, height) * 0.014;
  const anySelected = selectedRegionId !== null;
  return (
    <div className="sheet-canvas" style={{ aspectRatio: `${width} / ${height}` }}>
      <img src={api.imageUrl(sheetId)} alt="Herbarium sheet" draggable={false} />
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label="Detected regions">
        {anySelected && (
          <path
            // Dim everything except the selected region
            d={`M0 0H${width}V${height}H0Z ${regions
              .filter((r) => r.id === selectedRegionId)
              .map((r) => `M${r.x1} ${r.y1}V${r.y2}H${r.x2}V${r.y1}Z`)
              .join(" ")}`}
            fill="rgba(20,20,20,0.35)"
            fillRule="evenodd"
            pointerEvents="none"
          />
        )}
        {regions.map((r) => {
          const color = KIND_COLORS[r.kind];
          const selected = r.id === selectedRegionId;
          const tag = `${r.ordinal ? `R${r.ordinal} ` : ""}${KIND_NAMES[r.kind]}${r.score != null ? ` ${Math.round(r.score * 100)}%` : ""}`;
          const tagY = Math.max(0, r.y1 - font * 1.5);
          return (
            <g
              key={r.id}
              className={`region ${selected ? "selected" : ""}`}
              onClick={() => onSelectRegion?.(r.id)}
              style={{ cursor: onSelectRegion ? "pointer" : "default" }}
            >
              <rect
                x={r.x1}
                y={r.y1}
                width={r.x2 - r.x1}
                height={r.y2 - r.y1}
                fill={selected ? "rgba(255,255,255,0)" : `${color}14`}
                stroke={color}
                strokeWidth={selected ? 4 : 2}
                vectorEffect="non-scaling-stroke"
                strokeDasharray={r.source === "sam" ? undefined : "8 6"}
              />
              <rect x={r.x1} y={tagY} width={tag.length * font * 0.58 + font * 0.8} height={font * 1.4} fill={color} />
              <text x={r.x1 + font * 0.4} y={tagY + font * 1.05} fontSize={font} fill="#fff" fontFamily="system-ui, sans-serif">
                {tag}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
