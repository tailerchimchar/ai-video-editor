import { motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { Badge } from "./ui/Badge";
import { formatDuration } from "@/lib/time";
import { cn } from "@/lib/cn";
import type { Clip } from "@/types/clip";

interface ClipFilmstripProps {
  clips: Clip[];
  /** UUID of the currently-selected clip (user clicked to edit). */
  selectedId: string | null;
  /** UUID of the clip the VIDEO is currently playing through — different
   *  from selected. Drives the auto-scroll + "now playing" highlight. */
  playingId: string | null;
  /** Folder URL prefix where the per-clip thumbnails live. */
  thumbnailsBaseUrl: string | null;
  /** Cache-buster — bumps when a re-render regenerates thumbs. */
  cacheBust: number | string;
  /** Disable all gestures while a mutation is in flight for this clip. */
  busy: boolean;
  onSelect: (clipId: string) => void;
  /**
   * Edge-drag extend/shrink commit. `before` is seconds added BEFORE the
   * clip's source start (positive=extend, negative=trim). `after` is
   * seconds added AFTER. Fires on mouseup; the panel handles validation.
   */
  onExtend: (clipId: string, deltas: { before: number; after: number }) => void;
  /**
   * Drag-and-drop reorder commit. `newOrder` is the full clip-id list in
   * its new sequence. Fires once on drop.
   */
  onReorder: (newOrder: string[]) => void;
}

/**
 * Horizontal scrollable strip of clip tiles with two gestures:
 *
 * 1. **Edge drag** (left/right edge handles, ew-resize cursor) —
 *    extends or shrinks the clip's source window. Replaces the old
 *    ExtendSlider in ClipMetaPanel. Pixels-per-second is derived from
 *    the tile's rendered width / clip duration, so a 10px drag on a
 *    176px tile that's 8s long = ~0.45s delta.
 *
 * 2. **Middle drag** (whole tile body, grab cursor) — drag-to-reorder.
 *    HTML5 native drag-and-drop; we compute the new order based on the
 *    drop position relative to other tiles and emit one onReorder call.
 *
 * Click (no drag) still selects the clip for editing. The two drag
 * gestures are mutually exclusive via the mousedown target — handles
 * are above the body in the z-stack and intercept first.
 */
export function ClipFilmstrip({
  clips,
  selectedId,
  playingId,
  thumbnailsBaseUrl,
  cacheBust,
  busy,
  onSelect,
  onExtend,
  onReorder,
}: ClipFilmstripProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  // Refs for each tile keyed by clip id — lets us scrollIntoView the
  // currently-playing tile without re-querying the DOM.
  const tileRefs = useRef<Map<string, HTMLDivElement>>(new Map());

  // Reorder state — which clip is being dragged, where we'd drop it.
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const [dropIndex, setDropIndex] = useState<number | null>(null);

  // Auto-scroll the playing tile so it's visible in the strip.
  useEffect(() => {
    if (!playingId) return;
    const tile = tileRefs.current.get(playingId);
    const container = scrollRef.current;
    if (!tile || !container) return;
    const target = tile.offsetLeft - (container.clientWidth - tile.clientWidth) / 2;
    const current = container.scrollLeft;
    if (Math.abs(current - target) < 4) return;
    container.scrollTo({ left: Math.max(0, target), behavior: "smooth" });
  }, [playingId]);

  // Vertical wheel → horizontal scroll (passive: false to allow preventDefault).
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    function onWheel(event: WheelEvent) {
      if (!el) return;
      if (event.deltaY === 0) return;
      if (Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
      event.preventDefault();
      el.scrollLeft += event.deltaY;
    }
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  if (clips.length === 0) {
    return (
      <div className="surface-elevated rounded p-6 font-mono text-xs text-text-muted">no clips</div>
    );
  }

  // Reorder handlers — HTML5 native drag-and-drop. The tile that's being
  // dragged sets draggingId; tiles that mouseover update dropIndex.
  function handleDragStart(clipId: string) {
    if (busy) return;
    setDraggingId(clipId);
  }

  function handleDragOver(e: React.DragEvent, index: number) {
    e.preventDefault(); // required to allow drop
    if (draggingId === null) return;
    // Decide left-half vs right-half of the hovered tile to set the
    // drop position more precisely.
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const isLeftHalf = e.clientX - rect.left < rect.width / 2;
    setDropIndex(isLeftHalf ? index : index + 1);
  }

  function handleDragEnd() {
    setDraggingId(null);
    setDropIndex(null);
  }

  function handleDrop() {
    if (draggingId === null || dropIndex === null) {
      handleDragEnd();
      return;
    }
    const currentOrder = clips.map((c) => c.id);
    const fromIdx = currentOrder.indexOf(draggingId);
    if (fromIdx < 0) {
      handleDragEnd();
      return;
    }
    // Compute new order. Adjust drop target if we're moving DOWN the
    // list — the array index shifts by 1 after the splice removes
    // the dragged item.
    const newOrder = [...currentOrder];
    newOrder.splice(fromIdx, 1);
    const adjustedDrop = dropIndex > fromIdx ? dropIndex - 1 : dropIndex;
    newOrder.splice(adjustedDrop, 0, draggingId);
    handleDragEnd();
    // Only fire if it actually changed.
    if (newOrder.some((id, i) => id !== currentOrder[i])) {
      onReorder(newOrder);
    }
  }

  return (
    <div className="surface-elevated overflow-hidden rounded-lg">
      <header className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="font-mono text-[10px] uppercase tracking-wider text-text-dim">
          filmstrip · {clips.length} clip{clips.length === 1 ? "" : "s"}
        </div>
        <div className="font-mono text-[10px] uppercase tracking-wider text-text-dim">
          click to edit · drag edges to extend · drag tile to reorder
        </div>
      </header>
      <div ref={scrollRef} className="flex gap-3 overflow-x-auto p-3">
        {clips.map((clip, i) => (
          <ClipTile
            key={clip.id}
            clip={clip}
            staggerIndex={i}
            index={i}
            isSelected={clip.id === selectedId}
            isPlaying={clip.id === playingId}
            isDragging={clip.id === draggingId}
            // Drop indicator shows on the LEFT of tile i (if dropIndex === i)
            // and on the RIGHT of the last tile (if dropIndex === clips.length).
            showDropIndicatorLeft={dropIndex === i}
            showDropIndicatorRight={dropIndex === clips.length && i === clips.length - 1}
            busy={busy}
            thumbnailsBaseUrl={thumbnailsBaseUrl}
            cacheBust={cacheBust}
            tileRef={(el) => {
              if (el) tileRefs.current.set(clip.id, el);
              else tileRefs.current.delete(clip.id);
            }}
            onSelect={onSelect}
            onExtend={onExtend}
            onDragStart={handleDragStart}
            onDragOver={handleDragOver}
            onDrop={handleDrop}
            onDragEnd={handleDragEnd}
          />
        ))}
      </div>
    </div>
  );
}

const TILE_WIDTH_PX = 176; // matches Tailwind w-44 (11rem * 16px)

interface ClipTileProps {
  clip: Clip;
  staggerIndex: number;
  index: number;
  isSelected: boolean;
  isPlaying: boolean;
  isDragging: boolean;
  showDropIndicatorLeft: boolean;
  showDropIndicatorRight: boolean;
  busy: boolean;
  thumbnailsBaseUrl: string | null;
  cacheBust: number | string;
  tileRef: (el: HTMLDivElement | null) => void;
  onSelect: (clipId: string) => void;
  onExtend: (clipId: string, deltas: { before: number; after: number }) => void;
  onDragStart: (clipId: string) => void;
  onDragOver: (e: React.DragEvent, index: number) => void;
  onDrop: () => void;
  onDragEnd: () => void;
}

function ClipTile({
  clip,
  staggerIndex,
  index,
  isSelected,
  isPlaying,
  isDragging,
  showDropIndicatorLeft,
  showDropIndicatorRight,
  busy,
  thumbnailsBaseUrl,
  cacheBust,
  tileRef,
  onSelect,
  onExtend,
  onDragStart,
  onDragOver,
  onDrop,
  onDragEnd,
}: ClipTileProps) {
  const thumbUrl = thumbnailsBaseUrl
    ? `${thumbnailsBaseUrl}/${encodeURIComponent(clip.id)}.jpg?v=${cacheBust}`
    : null;
  const isIntro = clip.event === "intro";

  // Edge-drag state — tracks which edge is being pulled, and the live
  // preview delta in seconds. On mouseup we commit via onExtend.
  const [edgeDrag, setEdgeDrag] = useState<{
    edge: "left" | "right";
    deltaSeconds: number;
  } | null>(null);

  const pxPerSecond = TILE_WIDTH_PX / Math.max(0.5, clip.duration);

  function startEdgeDrag(e: React.PointerEvent, edge: "left" | "right") {
    e.preventDefault();
    e.stopPropagation(); // don't let it bubble into the reorder dragstart
    if (busy) return;
    const startX = e.clientX;
    setEdgeDrag({ edge, deltaSeconds: 0 });

    function onMove(ev: PointerEvent) {
      const dxPx = ev.clientX - startX;
      const dxSec = dxPx / pxPerSecond;
      // Left edge: dragging LEFT (dxPx < 0) extends the clip backward
      // in source (before > 0). Dragging RIGHT trims (before < 0).
      // Right edge: dragging RIGHT (dxPx > 0) extends forward (after > 0).
      const deltaSeconds = edge === "left" ? -dxSec : dxSec;
      setEdgeDrag({ edge, deltaSeconds });
    }

    function onUp(ev: PointerEvent) {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      const dxPx = ev.clientX - startX;
      if (Math.abs(dxPx) < 3) {
        // Treated as a click on the edge — ignore (don't accidentally extend).
        setEdgeDrag(null);
        return;
      }
      const dxSec = dxPx / pxPerSecond;
      const beforeDelta = edge === "left" ? -dxSec : 0;
      const afterDelta = edge === "right" ? dxSec : 0;
      // Round to 0.1s precision — sub-frame nudges aren't meaningful.
      const before = Math.round(beforeDelta * 10) / 10;
      const after = Math.round(afterDelta * 10) / 10;
      setEdgeDrag(null);
      if (before !== 0 || after !== 0) {
        onExtend(clip.id, { before, after });
      }
    }

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  // Visual transform for the live edge-drag preview — scales/shifts the
  // tile to show the new effective duration without committing yet.
  const previewStyle: React.CSSProperties = (() => {
    if (!edgeDrag) return {};
    const widthChange = edgeDrag.deltaSeconds * pxPerSecond;
    if (edgeDrag.edge === "right") {
      return { width: `${TILE_WIDTH_PX + widthChange}px`, transition: "none" };
    }
    // Left-edge drag shifts the tile + grows width on the LEFT.
    return {
      width: `${TILE_WIDTH_PX + widthChange}px`,
      transform: `translateX(${-widthChange}px)`,
      transition: "none",
    };
  })();

  return (
    <div className="relative">
      {/* Drop indicator on the LEFT of this tile when something is being
       *  dragged HERE. 3px tall accent bar that says "insert here." */}
      {showDropIndicatorLeft && (
        <div className="absolute -left-2 top-0 z-10 h-full w-1 rounded bg-accent shadow-[0_0_8px_var(--accent)]" />
      )}
      {showDropIndicatorRight && (
        <div className="absolute -right-2 top-0 z-10 h-full w-1 rounded bg-accent shadow-[0_0_8px_var(--accent)]" />
      )}

      <motion.div
        ref={tileRef}
        draggable={!busy && !edgeDrag}
        // Motion's dragStart event type conflicts with HTML5's native
        // DragEvent — we know it's actually a DragEvent at runtime
        // because `draggable={true}` makes the browser fire the native
        // event. Cast through unknown to satisfy both types.
        onDragStart={(e) => {
          const dragEvent = e as unknown as React.DragEvent<HTMLDivElement>;
          if (dragEvent.dataTransfer) {
            dragEvent.dataTransfer.setData("text/plain", clip.id);
            dragEvent.dataTransfer.effectAllowed = "move";
          }
          onDragStart(clip.id);
        }}
        onDragOver={(e) => onDragOver(e as unknown as React.DragEvent, index)}
        onDrop={(e) => {
          (e as unknown as React.DragEvent).preventDefault();
          onDrop();
        }}
        onDragEnd={onDragEnd}
        onClick={() => {
          if (!edgeDrag) onSelect(clip.id);
        }}
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: isDragging ? 0.4 : 1, y: 0 }}
        transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1], delay: staggerIndex * 0.04 }}
        style={previewStyle}
        className={cn(
          "group relative flex w-44 shrink-0 cursor-grab flex-col gap-2 rounded-md p-2 text-left",
          "select-none border-2 transition-all duration-150",
          "active:cursor-grabbing",
          isSelected && [
            "border-accent",
            "shadow-[0_0_0_5px_rgba(96,165,250,0.4),0_0_18px_rgba(59,130,246,0.55)]",
            "bg-accent/5",
          ],
          !isSelected && "hover:bg-bg-overlay/40 border-border hover:border-border-strong",
          isPlaying && !isSelected && "bg-accent/10 border-accent/40",
          edgeDrag && "shadow-[0_0_0_2px_var(--accent)]",
        )}
      >
        {/* Left edge drag handle — 8px wide invisible strip. Cursor +
         *  hover hint surface it as draggable without visual clutter. */}
        <div
          aria-label="drag to extend before"
          onPointerDown={(e) => startEdgeDrag(e, "left")}
          className={cn(
            "absolute left-0 top-0 z-20 h-full w-2 cursor-ew-resize",
            "hover:bg-accent/40 transition-colors",
            edgeDrag?.edge === "left" && "bg-accent/40",
          )}
        />
        <div
          aria-label="drag to extend after"
          onPointerDown={(e) => startEdgeDrag(e, "right")}
          className={cn(
            "absolute right-0 top-0 z-20 h-full w-2 cursor-ew-resize",
            "hover:bg-accent/40 transition-colors",
            edgeDrag?.edge === "right" && "bg-accent/40",
          )}
        />

        <div
          className={cn(
            "relative aspect-video w-full overflow-hidden rounded bg-bg-base",
            "border border-border",
          )}
        >
          {thumbUrl ? (
            <img
              src={thumbUrl}
              alt=""
              loading="lazy"
              draggable={false}
              className="h-full w-full object-cover"
              onError={(e) => {
                (e.target as HTMLImageElement).style.display = "none";
              }}
            />
          ) : (
            <div className="flex h-full items-center justify-center font-mono text-[10px] text-text-dim">
              —
            </div>
          )}
          <div className="bg-bg-base/80 absolute left-1.5 top-1.5 flex items-center gap-1 rounded px-1.5 py-0.5 font-mono text-[10px] text-text-primary backdrop-blur-sm">
            {isPlaying && (
              <span
                aria-hidden
                className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent shadow-[0_0_4px_var(--accent)]"
              />
            )}
            #{String(clip.index).padStart(2, "0")}
          </div>
          <div className="bg-bg-base/80 absolute bottom-1.5 right-1.5 rounded px-1.5 py-0.5 font-mono text-[10px] text-text-primary backdrop-blur-sm">
            {formatDuration(clip.duration)}
          </div>
          {/* Live extend preview — shows the delta during drag so the
           *  user can see how much they're adding before commit. */}
          {edgeDrag && (
            <div
              className={cn(
                "absolute inset-x-0 bottom-0 flex items-center justify-center",
                "bg-accent/80 py-0.5 font-mono text-[10px] text-bg-base",
              )}
            >
              {edgeDrag.deltaSeconds > 0 ? "+" : ""}
              {edgeDrag.deltaSeconds.toFixed(1)}s {edgeDrag.edge === "left" ? "pre" : "post"}
            </div>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-1">
          {isIntro && <Badge tone="accent">intro</Badge>}
          {clip.effects.length > 0 && <Badge tone="neutral">{clip.effects.length} fx</Badge>}
          {clip.caption_segments.length > 0 && (
            <Badge tone="muted">
              {clip.caption_segments.length} cap{clip.caption_segments.length === 1 ? "" : "s"}
            </Badge>
          )}
        </div>
      </motion.div>
    </div>
  );
}
