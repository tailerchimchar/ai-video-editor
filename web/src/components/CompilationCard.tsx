import { useState } from "react";
import { motion } from "motion/react";
import { Link } from "react-router-dom";
import { relativeTime } from "@/lib/time";
import { parseCompilationTitle } from "@/lib/title";
import type { CompilationSummary } from "@/types/compilation";

interface CompilationCardProps {
  compilation: CompilationSummary;
  index: number;
}

/**
 * One tile in the compilations gallery. Thumbnail-first card layout.
 *
 * Thumbnail source: `<output_path_dir>/thumbnail.jpg` — extracted at
 * render time by the backend. Falls back to a placeholder gradient if
 * the file is missing (newly-created compilation, render-failed, etc).
 *
 * Entry stagger: 50ms per card so the grid populates left-to-right
 * top-to-bottom on first paint. `prefers-reduced-motion` strips it
 * via the global @media block in globals.css.
 */
export function CompilationCard({ compilation, index }: CompilationCardProps) {
  const shortId = compilation.id.slice(0, 8);
  const stem = compilation.output_path ? folderStem(compilation.output_path) : null;
  const title = parseCompilationTitle(stem);
  const thumbUrl = buildWorkspaceUrl(
    compilation.output_path?.replace(/compilation\.mp4$/, "thumbnail.jpg") ?? null,
  );
  // Track 404s so we can swap to the placeholder. Older compilations
  // (pre-thumbnail-extraction feature) have a valid URL but the file
  // doesn't exist — without this we'd just show a black square.
  const [thumbBroken, setThumbBroken] = useState(false);
  const showThumb = thumbUrl && !thumbBroken;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{
        duration: 0.45,
        ease: [0.22, 1, 0.36, 1],
        delay: Math.min(index, 12) * 0.05,
      }}
    >
      <Link
        to={`/compilations/${compilation.id}`}
        className="surface-interactive hover:border-accent/40 group block overflow-hidden rounded-lg border border-border bg-bg-elevated transition-all hover:shadow-[0_0_0_1px_var(--accent-glow)]"
      >
        {/* Thumbnail — 16:9. Falls back to a gradient placeholder if missing. */}
        <div className="relative aspect-video w-full overflow-hidden bg-bg-base">
          {showThumb ? (
            <img
              src={thumbUrl}
              alt=""
              className="absolute inset-0 h-full w-full object-cover transition-transform duration-500 group-hover:scale-[1.02]"
              loading="lazy"
              onError={() => setThumbBroken(true)}
            />
          ) : (
            <div className="absolute inset-0 flex items-center justify-center bg-gradient-to-br from-bg-overlay to-bg-base">
              <span className="font-mono text-[10px] uppercase tracking-wider text-text-dim">
                no thumbnail yet
              </span>
            </div>
          )}
          {/* Subtle bottom gradient so any caption text reads cleanly */}
          <div className="absolute inset-x-0 bottom-0 h-12 bg-gradient-to-t from-black/70 to-transparent" />
        </div>

        {/* Metadata strip below the thumbnail */}
        <div className="space-y-1.5 p-4">
          <div className="truncate text-sm text-text-primary">
            {title.game || stem || "(no output)"}
          </div>
          <div className="flex flex-wrap items-center gap-2 font-mono text-[10px] uppercase tracking-wider text-text-dim">
            {title.gameDate && (
              <>
                <span>played {title.gameDate}</span>
                <span className="text-text-dim/50">·</span>
              </>
            )}
            <span>{relativeTime(compilation.created_at)}</span>
            <span className="text-text-dim/50">·</span>
            <span className="text-text-muted">{shortId}</span>
          </div>
        </div>
      </Link>
    </motion.div>
  );
}

/**
 * Map the API's filesystem path to a URL the browser can load through
 * the Vite proxy. Slice off everything before `/compilations/` so any
 * user's workspace root works. Mirrors the helper in CompilationViewer.
 */
function buildWorkspaceUrl(outputPath: string | null | undefined): string | null {
  if (!outputPath) return null;
  const normalised = outputPath.replace(/\\/g, "/");
  const marker = "/compilations/";
  const idx = normalised.indexOf(marker);
  if (idx < 0) return null;
  const tail = normalised
    .slice(idx + marker.length)
    .split("/")
    .map(encodeURIComponent)
    .join("/");
  return `/workspace/compilations/${tail}`;
}

/**
 * Extract `<stem>` from `.../compilations/<stem>/compilation.mp4`.
 */
function folderStem(outputPath: string): string {
  const parts = outputPath.split(/[\\/]/);
  const idx = parts.lastIndexOf("compilation.mp4");
  if (idx > 0) {
    const parent = parts[idx - 1];
    if (parent) return parent;
  }
  return outputPath;
}
