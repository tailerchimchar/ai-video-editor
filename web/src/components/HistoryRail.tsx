import { motion } from "motion/react";
import { Button } from "./ui/Button";
import { relativeTime } from "@/lib/time";
import { cn } from "@/lib/cn";
import type { HistoryEntry } from "@/types/compilation";

interface HistoryRailProps {
  entries: HistoryEntry[];
  reverting: boolean;
  onRevert: (toVersion: number) => void;
}

/**
 * Vertical journal sidebar.
 *
 * Each entry shows version, action, relative time. Most recent at the
 * top. Clicking the revert button jumps back to that version (calls
 * POST /revert with `to_version`).
 *
 * The CURRENT version (highest number) is shown but not revertable —
 * you can't revert "to now."
 */
export function HistoryRail({ entries, reverting, onRevert }: HistoryRailProps) {
  if (entries.length === 0) {
    return (
      <div className="surface-elevated rounded p-4 font-mono text-xs text-text-muted">
        no edits yet
      </div>
    );
  }

  const newest = entries.at(-1)?.version ?? 0;

  return (
    <div className="surface-elevated rounded">
      <header className="border-b border-border px-4 py-3">
        <div className="font-mono text-[10px] uppercase tracking-wider text-text-dim">history</div>
      </header>
      <ol className="flex max-h-[480px] flex-col overflow-y-auto">
        {[...entries].reverse().map((entry, i) => {
          const isCurrent = entry.version === newest;
          return (
            <motion.li
              key={entry.version}
              initial={{ opacity: 0, x: -4 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.3, delay: i * 0.03 }}
              className={cn(
                "border-border/60 flex items-center gap-3 border-b px-4 py-3 last:border-b-0",
                isCurrent && "bg-bg-overlay/40",
              )}
            >
              <span className="font-mono text-[10px] text-text-dim">
                v{String(entry.version).padStart(2, "0")}
              </span>
              <div className="min-w-0 flex-1 space-y-0.5">
                {/* Backend's pretty `display` string when present;
                 *  fall back to the raw action key so old comps still
                 *  render something readable. */}
                <div className="truncate text-xs text-text-primary" title={entry.action}>
                  {entry.display ?? entry.action}
                </div>
                <div className="font-mono text-[10px] text-text-muted">
                  {relativeTime(entry.ts)}
                </div>
              </div>
              {!isCurrent && (
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={reverting}
                  onClick={() => onRevert(entry.version)}
                >
                  revert
                </Button>
              )}
            </motion.li>
          );
        })}
      </ol>
    </div>
  );
}
