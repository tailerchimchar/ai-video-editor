import { motion } from "motion/react";
import { Badge } from "./ui/Badge";
import { formatDuration } from "@/lib/time";
import { cn } from "@/lib/cn";
import type { Clip } from "@/types/clip";

interface ClipMetaPanelProps {
  clip: Clip;
  busy: boolean;
}

/**
 * LEFT-column clip identity card: index, timestamps, badges.
 *
 * Editing previously lived here (the ExtendSlider). With the filmstrip
 * refactor, extend/shrink now happens by dragging the edges of the
 * tile directly — so this panel is purely informational. Caption
 * editing + effects stay on the right in `ClipActionsPanel`.
 *
 * The `busy` indicator shows during any mutation in flight for this
 * clip (extend / caption / effect / etc) so the user gets the same
 * "something is happening" visual cue.
 */
export function ClipMetaPanel({ clip, busy }: ClipMetaPanelProps) {
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
        "surface-elevated space-y-4 rounded-lg p-6",
        "transition-shadow duration-300",
        busy && "shadow-[0_0_0_1px_var(--accent-glow),inset_0_1px_0_rgba(255,255,255,0.06)]",
      )}
    >
      <header className="flex items-start justify-between gap-4">
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
          <div className="pt-1 font-mono text-[10px] text-text-dim">
            drag the tile edges in the filmstrip above to extend or shrink
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
    </motion.section>
  );
}
