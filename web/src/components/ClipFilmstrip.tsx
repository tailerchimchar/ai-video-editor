import { motion } from "motion/react";
import { useEffect, useRef } from "react";
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
  onSelect: (clipId: string) => void;
}

/**
 * Horizontal scrollable strip of clip tiles.
 *
 * Each tile shows: thumbnail frame, index `#NN`, duration, event badge,
 * and the count of any active effects. Click selects the clip — the
 * detail panel below opens with the slider + actions for it.
 *
 * Scaling: at 160px per tile (compact card), 30 clips fit in roughly
 * 4800px of horizontal scroll. That's well-handled by overflow-x;
 * Phase 2 can virtualise if it ever becomes a problem.
 */
export function ClipFilmstrip({
  clips,
  selectedId,
  playingId,
  thumbnailsBaseUrl,
  cacheBust,
  onSelect,
}: ClipFilmstripProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  // Refs for each tile keyed by clip id — lets us scrollIntoView the
  // currently-playing tile without re-querying the DOM. `useRef<Map>`
  // pattern is more stable than recreating refs on every render.
  const tileRefs = useRef<Map<string, HTMLButtonElement>>(new Map());

  // Auto-scroll the playing tile so it's visible in the strip.
  //
  // We do this with manual `scrollTo` instead of `scrollIntoView` for
  // two reasons:
  //   1. `inline: center` behavior is inconsistent across browsers
  //      (Chrome occasionally ignores it).
  //   2. `scrollIntoView` can bubble up and scroll the PAGE, which we
  //      don't want — only the filmstrip container should move.
  //
  // The target offset centers the tile horizontally in the strip. We
  // only fire when the tile is meaningfully off-center so this doesn't
  // fight the user during manual horizontal scrolling.
  useEffect(() => {
    if (!playingId) return;
    const tile = tileRefs.current.get(playingId);
    const container = scrollRef.current;
    if (!tile || !container) return;
    const target = tile.offsetLeft - (container.clientWidth - tile.clientWidth) / 2;
    const current = container.scrollLeft;
    if (Math.abs(current - target) < 4) return; // already centered enough
    container.scrollTo({ left: Math.max(0, target), behavior: "smooth" });
  }, [playingId]);

  // Vertical wheel → horizontal scroll. React's onWheel handler is
  // passive by default (preventDefault is silently ignored), so we
  // attach manually via addEventListener with `passive: false`.
  //
  // We don't pre-check for overflow — `scrollLeft +=` on a non-overflowing
  // element is a no-op. We DO skip when the user is already scrolling
  // horizontally (trackpad two-finger pan: |deltaX| > |deltaY|), so
  // native horizontal scroll keeps working.
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

  return (
    <div className="surface-elevated overflow-hidden rounded-lg">
      <header className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="font-mono text-[10px] uppercase tracking-wider text-text-dim">
          filmstrip · {clips.length} clip{clips.length === 1 ? "" : "s"}
        </div>
        <div className="font-mono text-[10px] uppercase tracking-wider text-text-dim">
          click to edit · scroll to pan
        </div>
      </header>
      {/* Native horizontal scroll; explicit pb keeps the scrollbar
       *  from clipping the thumbnails' bottom shadow. */}
      <div ref={scrollRef} className="flex gap-3 overflow-x-auto p-3">
        {clips.map((clip, i) => (
          <ClipTile
            key={clip.id}
            clip={clip}
            staggerIndex={i}
            isSelected={clip.id === selectedId}
            isPlaying={clip.id === playingId}
            thumbnailsBaseUrl={thumbnailsBaseUrl}
            cacheBust={cacheBust}
            tileRef={(el) => {
              if (el) tileRefs.current.set(clip.id, el);
              else tileRefs.current.delete(clip.id);
            }}
            onSelect={onSelect}
          />
        ))}
      </div>
    </div>
  );
}

interface ClipTileProps {
  clip: Clip;
  staggerIndex: number;
  isSelected: boolean;
  isPlaying: boolean;
  thumbnailsBaseUrl: string | null;
  cacheBust: number | string;
  /** Ref callback so the parent can scrollIntoView the playing tile. */
  tileRef: (el: HTMLButtonElement | null) => void;
  onSelect: (clipId: string) => void;
}

function ClipTile({
  clip,
  staggerIndex,
  isSelected,
  isPlaying,
  thumbnailsBaseUrl,
  cacheBust,
  tileRef,
  onSelect,
}: ClipTileProps) {
  const thumbUrl = thumbnailsBaseUrl
    ? `${thumbnailsBaseUrl}/${encodeURIComponent(clip.id)}.jpg?v=${cacheBust}`
    : null;
  const isIntro = clip.event === "intro";

  return (
    <motion.button
      ref={tileRef}
      type="button"
      onClick={() => onSelect(clip.id)}
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1], delay: staggerIndex * 0.04 }}
      className={cn(
        "group relative flex w-44 shrink-0 flex-col gap-2 rounded-md p-2 text-left",
        "border-2 transition-all duration-150",
        // Selected = user clicked to edit this clip. Bold light-blue
        // border + ring so it's obvious which clip the right-side
        // editor is targeting. The 5px outer ring uses the accent
        // colour at high opacity for visibility against the dark bg.
        isSelected && [
          "border-accent",
          "shadow-[0_0_0_5px_rgba(96,165,250,0.4),0_0_18px_rgba(59,130,246,0.55)]",
          "bg-accent/5",
        ],
        // Playing = video playhead is in this clip. Subtle bg tint
        // + pulse dot in the overlay. Coexists with selected so the
        // tile can show both states at once.
        !isSelected && "hover:bg-bg-overlay/40 border-border hover:border-border-strong",
        isPlaying && !isSelected && "bg-accent/10 border-accent/40",
      )}
    >
      {/* 16:9 thumbnail tile. The browser auto-loads `_thumbnails/<id>.jpg`
       *  from the workspace mount; missing files show the fallback. */}
      <div
        className={cn(
          "relative aspect-video w-full overflow-hidden rounded bg-bg-base",
          "border border-border",
        )}
      >
        {thumbUrl ? (
          // Show the real rendered frame for every clip type — intros
          // included. The intro's midpoint frame is the Noodlz logo,
          // which is more useful than a placeholder. Fallback to the
          // bg-bg-base + "—" only if the file actually fails to load.
          <img
            src={thumbUrl}
            alt=""
            loading="lazy"
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
        {/* Top-left overlay: clip index + playing indicator */}
        <div className="bg-bg-base/80 absolute left-1.5 top-1.5 flex items-center gap-1 rounded px-1.5 py-0.5 font-mono text-[10px] text-text-primary backdrop-blur-sm">
          {isPlaying && (
            <span
              aria-hidden
              className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent shadow-[0_0_4px_var(--accent)]"
            />
          )}
          #{String(clip.index).padStart(2, "0")}
        </div>
        {/* Bottom-right overlay: duration */}
        <div className="bg-bg-base/80 absolute bottom-1.5 right-1.5 rounded px-1.5 py-0.5 font-mono text-[10px] text-text-primary backdrop-blur-sm">
          {formatDuration(clip.duration)}
        </div>
      </div>

      {/* Per-tile metadata row — kept minimal so the tile stays compact. */}
      <div className="flex flex-wrap items-center gap-1">
        {isIntro && <Badge tone="accent">intro</Badge>}
        {clip.effects.length > 0 && <Badge tone="neutral">{clip.effects.length} fx</Badge>}
        {clip.caption_segments.length > 0 && (
          <Badge tone="muted">
            {clip.caption_segments.length} cap{clip.caption_segments.length === 1 ? "" : "s"}
          </Badge>
        )}
      </div>
    </motion.button>
  );
}
