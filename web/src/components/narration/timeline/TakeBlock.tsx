import { useEffect, useRef, useState, type PointerEvent } from "react";
import { cn } from "@/lib/cn";
import {
  MIN_TAKE_S,
  effectiveEnd,
  effectiveStart,
  nudge,
  trimHead,
  trimTail,
} from "@/lib/takeEdits";
import { sToPx, type Viewport } from "@/lib/timeline";
import { columnPeaks } from "@/lib/waveform";
import type { Take, TakeEdit } from "@/types/narration";
import { LANE_HEIGHT, cssVar, drawMirrorBars, sizeCanvas } from "./lane";

/** A decoded take's waveform envelope, sampled at `rateHz`. */
export interface TakeWave {
  env: Float32Array;
  rateHz: number;
  /** Length of the whole recorded media, seconds. */
  mediaDurationS: number;
}

interface TakeBlockProps {
  take: Take;
  view: Viewport;
  selected: boolean;
  wave: TakeWave | undefined;
  /** Click without dragging. */
  onClick: (take: Take) => void;
  /** A drag finished: one edit for the whole gesture. */
  onEdit: (take: Take, edit: TakeEdit) => void;
}

type Mode = "nudge" | "head" | "tail";

interface Drag {
  mode: Mode;
  x0: number;
  origin: Take;
  moved: boolean;
}

const DRAG_THRESHOLD_PX = 3;
const BLOCK_PAD_Y = 4;

/** Where a drag of `ds` seconds puts the take (the server's limits applied). */
function dragged(d: Drag, ds: number, mediaDurationS: number | undefined): Take {
  const o = d.origin;
  if (d.mode === "nudge") return nudge(o, Math.max(o.offset_s + ds, -o.start_s));
  if (d.mode === "head")
    return trimHead(o, Math.min(effectiveStart(o) + ds, effectiveEnd(o) - MIN_TAKE_S));
  return trimTail(
    o,
    Math.max(effectiveEnd(o) + ds, effectiveStart(o) + MIN_TAKE_S),
    mediaDurationS,
  );
}

const signed = (s: number) => `${s >= 0 ? "+" : "−"}${Math.abs(s).toFixed(1)} s`;

/**
 * One take on the takes lane: waveform, camera / muted / selected styling.
 * Drag the body = nudge; drag an edge of the selected take = trim. The
 * drag renders a local draft and commits one edit on release.
 */
export function TakeBlock({ take, view, selected, wave, onClick, onEdit }: TakeBlockProps) {
  const [draft, setDraft] = useState<Take | null>(null);
  const dragRef = useRef<Drag | null>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const shown = draft ?? take;

  const left = sToPx(view, effectiveStart(shown));
  const width = Math.max(2, shown.duration_s * view.pxPerS);
  const height = LANE_HEIGHT.takes - BLOCK_PAD_Y * 2;
  // Only the part of the block inside the viewport gets a canvas.
  const visL = Math.max(0, left);
  const visR = Math.min(view.widthPx, left + width);
  const visW = Math.floor(visR - visL);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !wave || visW <= 0) return;
    const g = sizeCanvas(canvas, visW, height - 2);
    if (!g) return;
    const fromS = shown.media_in_s + (visL - left) / view.pxPerS;
    const peaks = columnPeaks(wave.env, wave.rateHz, fromS, view.pxPerS, visW);
    drawMirrorBars(g, peaks, height - 2, cssVar(shown.muted ? "--text-dim" : "--accent"));
  }, [wave, shown.media_in_s, shown.muted, visL, visW, left, view.pxPerS, height]);

  function onPointerDown(e: PointerEvent<HTMLDivElement>) {
    if (e.button !== 0) return;
    e.stopPropagation();
    const edge = (e.target as HTMLElement).dataset.edge as Mode | undefined;
    e.currentTarget.setPointerCapture(e.pointerId);
    dragRef.current = { mode: edge ?? "nudge", x0: e.clientX, origin: take, moved: false };
  }

  function onPointerMove(e: PointerEvent<HTMLDivElement>) {
    const d = dragRef.current;
    if (!d) return;
    const dx = e.clientX - d.x0;
    if (!d.moved && Math.abs(dx) < DRAG_THRESHOLD_PX) return;
    d.moved = true;
    setDraft(dragged(d, dx / view.pxPerS, wave?.mediaDurationS));
  }

  function onPointerUp(e: PointerEvent<HTMLDivElement>) {
    const d = dragRef.current;
    dragRef.current = null;
    if (!d) return;
    setDraft(null);
    if (!d.moved) {
      onClick(take);
      return;
    }
    const next = dragged(d, (e.clientX - d.x0) / view.pxPerS, wave?.mediaDurationS);
    if (d.mode === "nudge") {
      if (next.offset_s !== d.origin.offset_s) onEdit(take, { offset_s: next.offset_s });
    } else if (next.duration_s !== d.origin.duration_s) {
      onEdit(take, {
        start_s: next.start_s,
        media_in_s: next.media_in_s,
        duration_s: next.duration_s,
      });
    }
  }

  function onPointerCancel() {
    dragRef.current = null;
    setDraft(null);
  }

  const d = dragRef.current;
  const label =
    draft && d
      ? d.mode === "nudge"
        ? signed(draft.offset_s - d.origin.offset_s)
        : `${draft.duration_s.toFixed(1)} s`
      : null;

  return (
    <div
      className={cn(
        "absolute touch-none rounded-sm border bg-accent-glow",
        draft && d?.mode === "nudge" ? "cursor-grabbing" : "cursor-grab",
        take.has_camera ? "border-success" : "border-accent",
        take.muted && "border-border-strong bg-bg-overlay opacity-50",
        selected && "z-10 outline outline-1 outline-text-primary",
      )}
      style={{ left, width, top: BLOCK_PAD_Y, height }}
      title={`${effectiveStart(shown).toFixed(1)} s – ${effectiveEnd(shown).toFixed(1)} s${take.muted ? " · muted" : ""}`}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
    >
      {visW > 0 && wave && (
        <canvas
          ref={canvasRef}
          className="pointer-events-none absolute top-0"
          style={{ left: visL - left }}
        />
      )}
      {selected && width >= 12 && (
        <>
          <div
            data-edge="head"
            className="absolute inset-y-0 left-0 w-1.5 cursor-ew-resize bg-text-primary opacity-60 hover:opacity-100"
          />
          <div
            data-edge="tail"
            className="absolute inset-y-0 right-0 w-1.5 cursor-ew-resize bg-text-primary opacity-60 hover:opacity-100"
          />
        </>
      )}
      {label && (
        <span className="pointer-events-none absolute left-2 top-0.5 rounded-sm bg-bg-base px-1 font-mono text-[10px] text-text-primary">
          {label}
        </span>
      )}
    </div>
  );
}
