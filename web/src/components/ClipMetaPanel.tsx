import { motion } from "motion/react";
import { ExtendSlider } from "./ExtendSlider";
import { Badge } from "./ui/Badge";
import { formatDuration } from "@/lib/time";
import { cn } from "@/lib/cn";
import type { Clip } from "@/types/clip";

interface ReelSegment {
  id: string;
  reelStart: number;
  reelEnd: number;
  isCurrent: boolean;
}

interface ClipMetaPanelProps {
  clip: Clip;
  reelStart: number;
  reelEnd: number;
  totalReelSeconds: number;
  segments: ReelSegment[];
  busy: boolean;
  onExtend: (deltas: { before: number; after: number }) => void;
}

/**
 * LEFT-column clip panel: identity (index, timestamps, badges) + the
 * extend/shrink reel slider. The "what does this clip look like in
 * the reel?" surface — paired with the video player above it.
 *
 * Caption editing + action stubs live separately in `ClipActionsPanel`
 * on the right. Splitting these makes the captions surface front-and-
 * center on the right where editors expect their primary work area.
 */
export function ClipMetaPanel({
  clip,
  reelStart,
  reelEnd,
  totalReelSeconds,
  segments,
  busy,
  onExtend,
}: ClipMetaPanelProps) {
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
        "surface-elevated space-y-5 rounded-lg p-6",
        "transition-shadow duration-300",
        busy && "shadow-[0_0_0_1px_var(--accent-glow),inset_0_1px_0_rgba(255,255,255,0.06)]",
      )}
    >
      <header className="flex items-start justify-between gap-4 border-b border-border pb-4">
        <div className="space-y-1">
          <div className="font-mono text-[10px] uppercase tracking-wider text-text-dim">
            selected clip
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
    </motion.section>
  );
}
