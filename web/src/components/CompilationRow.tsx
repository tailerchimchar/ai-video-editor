import { motion } from "motion/react";
import { Link } from "react-router-dom";
import { relativeTime } from "@/lib/time";
import type { CompilationSummary } from "@/types/compilation";

interface CompilationRowProps {
  compilation: CompilationSummary;
  index: number;
}

/**
 * One row in the compilations list. Mono ID for craft-tool identity,
 * serif/sans hybrid for the asset filename, muted relative date.
 *
 * Entry stagger: 60ms per row per the design plan. Disabled
 * automatically when prefers-reduced-motion is set (via the global
 * @media block).
 */
export function CompilationRow({ compilation, index }: CompilationRowProps) {
  const shortId = compilation.id.slice(0, 8);
  // The asset filename isn't directly on the summary, but the output_path
  // contains the asset stem as the folder name — useful identity until we
  // expose the asset filename in the API response.
  const stem = compilation.output_path
    ? folderStem(compilation.output_path)
    : "(no rendered output yet)";

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{
        duration: 0.4,
        ease: [0.22, 1, 0.36, 1],
        delay: index * 0.06,
      }}
    >
      <Link
        to={`/compilations/${compilation.id}`}
        className="surface-interactive group flex items-center gap-6 rounded border border-border bg-bg-elevated px-6 py-5"
      >
        <span className="font-mono text-xs text-text-dim">
          {String(index + 1).padStart(2, "0")}
        </span>
        <div className="min-w-0 flex-1 space-y-1">
          <div className="truncate text-sm text-text-primary">{stem}</div>
          <div className="flex items-center gap-3 font-mono text-xs text-text-muted">
            <span>{shortId}</span>
            <span className="text-text-dim">·</span>
            <span>{relativeTime(compilation.created_at)}</span>
          </div>
        </div>
        <span className="font-mono text-xs text-text-dim opacity-0 transition-opacity group-hover:opacity-100">
          open ↗
        </span>
      </Link>
    </motion.div>
  );
}

/**
 * Extract the compilation folder stem from `output_path`. The path
 * shape is `.../compilations/<stem>/compilation.mp4` — we want
 * `<stem>` which encodes the asset identifier and timestamp.
 */
function folderStem(outputPath: string): string {
  const parts = outputPath.split(/[\\/]/);
  // Walk backwards to find the parent folder of compilation.mp4.
  const idx = parts.lastIndexOf("compilation.mp4");
  if (idx > 0) {
    const parent = parts[idx - 1];
    if (parent) return parent;
  }
  return outputPath;
}
