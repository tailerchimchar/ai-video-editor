import { memo, useMemo } from "react";
import { cn } from "@/lib/cn";
import { mmss } from "@/lib/time";
import { pxToS, sToPx, type Viewport } from "@/lib/timeline";
import type { Marker } from "@/types/narration";
import { LANE_HEIGHT, laneX } from "./lane";

interface MarkersLaneProps {
  view: Viewport;
  markers: Marker[];
  onSeek: (s: number) => void;
}

/** Markers at one second of one row, shown as a single tick. */
interface Group {
  atS: number;
  kind: Marker["kind"];
  labels: string[];
}

const KIND_STYLE: Record<Marker["kind"], string> = {
  game: "border-accent text-accent",
  chapter: "border-text-muted text-text-muted",
  insert: "border-dashed border-text-muted text-text-muted",
  cue: "border-text-dim text-text-dim",
};
/** Which kind styles a merged tick: the first present in this order. */
const KIND_RANK: Marker["kind"][] = ["game", "chapter", "insert", "cue"];
const SAME_S = 0.05;

/** Sort and merge the markers that share a second (e.g. a game start and its chapter). */
function groups(markers: Marker[]): Group[] {
  const out: Group[] = [];
  for (const m of [...markers].sort((a, b) => a.at_s - b.at_s)) {
    const last = out[out.length - 1];
    if (last && m.at_s - last.atS < SAME_S) {
      last.labels.push(m.label);
      if (KIND_RANK.indexOf(m.kind) < KIND_RANK.indexOf(last.kind)) last.kind = m.kind;
    } else {
      out.push({ atS: m.at_s, kind: m.kind, labels: [m.label] });
    }
  }
  return out;
}

/**
 * Two rows: chapters, game starts and inserts on top, cues below. Each
 * label is cut where the next one in its row starts (full text in the
 * tooltip). Click a marker or the lane = seek.
 */
export const MarkersLane = memo(function MarkersLane({ view, markers, onSeek }: MarkersLaneProps) {
  const rows = useMemo(
    () => [
      groups(markers.filter((m) => m.kind !== "cue")),
      groups(markers.filter((m) => m.kind === "cue")),
    ],
    [markers],
  );
  const rowH = LANE_HEIGHT.markers / 2;

  return (
    <div
      className="relative cursor-pointer overflow-hidden border-b border-border"
      style={{ height: LANE_HEIGHT.markers }}
      onPointerDown={(e) => onSeek(pxToS(view, laneX(e, e.currentTarget)))}
    >
      {rows.map((row, r) =>
        row.map((g, i) => {
          const x = sToPx(view, g.atS);
          const next = row[i + 1];
          const nextX = next ? sToPx(view, next.atS) : view.widthPx;
          if (x > view.widthPx || nextX < 0) return null;
          return (
            <button
              key={`${r}-${g.atS}`}
              type="button"
              title={`${mmss(g.atS)} · ${g.labels.join(" · ")}`}
              className={cn(
                "absolute truncate border-l pl-1 text-left font-mono text-[10px] hover:text-text-primary",
                KIND_STYLE[g.kind],
              )}
              style={{
                left: x,
                top: r * rowH,
                height: rowH,
                lineHeight: `${rowH}px`,
                maxWidth: Math.max(3, nextX - x - 2),
              }}
              onPointerDown={(e) => {
                e.stopPropagation();
                onSeek(g.atS);
              }}
            >
              {g.labels.join(" · ")}
            </button>
          );
        }),
      )}
    </div>
  );
});
