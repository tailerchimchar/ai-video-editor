import { motion } from "motion/react";
import { CaptionEditor } from "./CaptionEditor";
import { ExtendSlider } from "./ExtendSlider";
import { Badge } from "./ui/Badge";
import { Button } from "./ui/Button";
import { formatDuration } from "@/lib/time";
import { cn } from "@/lib/cn";
import type { CaptionSegment, Clip } from "@/types/clip";

interface ReelSegment {
  id: string;
  reelStart: number;
  reelEnd: number;
  isCurrent: boolean;
}

interface ClipDetailPanelProps {
  clip: Clip;
  /** Clip's source-range start/end seconds (parsed from `clip.source` `"M:SS"` string). */
  clipSourceStart: number;
  clipSourceEnd: number;
  reelStart: number;
  reelEnd: number;
  totalReelSeconds: number;
  segments: ReelSegment[];
  busy: boolean;
  /** Separate flag — any caption mutation in flight. */
  captionsBusy: boolean;
  onExtend: (deltas: { before: number; after: number }) => void;
  onEditCaptions: (segments: CaptionSegment[]) => void;
  onAddCaption: (args: { startSeconds: number; endSeconds: number; text: string }) => void;
  onRemoveCaption: (segmentIndex: number) => void;
  onTiktokify: () => void;
}

/**
 * Focused editor panel for the currently-selected clip.
 *
 * The detail panel replaces the per-clip-card layout — exactly ONE
 * panel is visible at a time (whichever clip is selected in the
 * filmstrip). This is the surface where every per-clip mutation
 * lives: extend/shrink (built), zoom/focus/caption_mode (stubbed
 * for future phases), remove.
 *
 * Stub buttons are intentional: they advertise the future feature
 * surface to the user without doing anything until those phases ship.
 * Keeps the design contract visible.
 */
export function ClipDetailPanel({
  clip,
  clipSourceStart,
  clipSourceEnd,
  reelStart,
  reelEnd,
  totalReelSeconds,
  segments,
  busy,
  captionsBusy,
  onExtend,
  onEditCaptions,
  onAddCaption,
  onRemoveCaption,
  onTiktokify,
}: ClipDetailPanelProps) {
  const [reelStartStr, reelEndStr] = clip.reel.split("-").map((s) => s.trim()) as [
    string | undefined,
    string | undefined,
  ];
  const [srcStart, srcEnd] = clip.source.split("-").map((s) => s.trim()) as [
    string | undefined,
    string | undefined,
  ];
  const isIntro = clip.event === "intro";

  return (
    <motion.section
      key={clip.id}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
      className={cn(
        "surface-elevated space-y-6 rounded-lg p-6",
        "transition-shadow duration-300",
        busy && "shadow-[0_0_0_1px_var(--accent-glow),inset_0_1px_0_rgba(255,255,255,0.06)]",
      )}
    >
      <header className="flex items-start justify-between gap-4 border-b border-border pb-4">
        <div className="space-y-1">
          <div className="font-mono text-[10px] uppercase tracking-wider text-text-dim">
            editing clip
          </div>
          <div className="flex items-baseline gap-3">
            <span className="font-mono text-2xl text-text-primary">
              #{String(clip.index).padStart(2, "0")}
            </span>
            <span className="font-mono text-base text-text-muted">
              {reelStartStr} → {reelEndStr}
            </span>
            <span className="font-mono text-xs text-text-dim">{formatDuration(clip.duration)}</span>
          </div>
          <div className="font-mono text-xs text-text-muted">
            src · {srcStart} → {srcEnd}
          </div>
        </div>

        <div className="flex shrink-0 flex-wrap items-center justify-end gap-1.5">
          {isIntro && <Badge tone="accent">intro</Badge>}
          {clip.effects.map((effect, i) => (
            <Badge key={`${effect.kind}-${i}`} tone="neutral">
              {effect.kind}
            </Badge>
          ))}
          {clip.caption_segments.length > 0 && (
            <Badge tone="muted">
              {clip.caption_segments.length} cap{clip.caption_segments.length === 1 ? "" : "s"}
            </Badge>
          )}
        </div>
      </header>

      <div>
        <div className="mb-3 font-mono text-[10px] uppercase tracking-wider text-text-dim">
          extend or shrink
        </div>
        <ExtendSlider
          clipReelStart={reelStart}
          clipReelEnd={reelEnd}
          totalReelSeconds={totalReelSeconds}
          segments={segments}
          disabled={busy}
          onCommit={onExtend}
        />
      </div>

      <div className="border-t border-border pt-4">
        <CaptionEditor
          initialSegments={clip.caption_segments}
          captionMode={clip.caption_mode}
          clipSourceStart={clipSourceStart}
          clipSourceEnd={clipSourceEnd}
          busy={captionsBusy}
          onSave={onEditCaptions}
          onAdd={onAddCaption}
          onRemove={onRemoveCaption}
          onTiktokify={onTiktokify}
        />
      </div>

      {/* Future-feature stubs. Keeping the surface visible so users see
       *  what's coming AND so accidental clicks are caught here vs an
       *  empty panel. Wire them up phase-by-phase. */}
      <div className="border-t border-border pt-4">
        <div className="mb-3 font-mono text-[10px] uppercase tracking-wider text-text-dim">
          more actions · coming soon
        </div>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="ghost" disabled>
            add zoom
          </Button>
          <Button size="sm" variant="ghost" disabled>
            add focus
          </Button>
          <Button size="sm" variant="ghost" disabled>
            tiktok captions
          </Button>
          <Button size="sm" variant="ghost" disabled>
            mark speaker
          </Button>
          <Button size="sm" variant="danger" disabled>
            remove clip
          </Button>
        </div>
      </div>
    </motion.section>
  );
}
