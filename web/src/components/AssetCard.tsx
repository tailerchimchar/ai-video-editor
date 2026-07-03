import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { motion } from "motion/react";
import { Badge } from "./ui/Badge";
import { Button } from "./ui/Button";
import { assetThumbnailUrl, getJob, splitVodIntoGames } from "@/api/assets";
import { relativeTime } from "@/lib/time";
import type { AssetSummary } from "@/api/assets";

interface AssetCardProps {
  asset: AssetSummary;
  index: number;
}

// One hour. Single League or Val games are 30-50 min; only recordings
// longer than this could plausibly contain multiple games back-to-back
// (scrim VODs, multi-game Twitch sessions, tournament series).
const MIN_SPLIT_DURATION_SECONDS = 3600;

// Split button eligibility. The blackdetect scan COULD handle short
// files (just returns "no split needed"), but showing the button on
// every Outplayed event clip adds visual noise without value. So we
// hide it unless:
//
// - The source file exists (not deleted).
// - This asset isn't already a split child (no recursive splitting).
// - We know the file is longer than 1hr (multi-game capable).
//
// Duration is NULL on pre-backfill rows. We treat unknown as "don't
// show" — better to under-promise than offer a button that won't work.
// Hit POST /assets/backfill_durations to populate the existing library.
function canSplit(asset: AssetSummary): boolean {
  if (asset.source_deleted_at) return false;
  if (asset.parent_asset_id) return false;
  const dur = asset.duration_seconds;
  if (dur == null || dur <= MIN_SPLIT_DURATION_SECONDS) return false;
  return true;
}

/** "1h 47m" / "32m" / "1:23" — null/zero render as empty string. */
function formatDuration(seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0) return "";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return `${s}s`;
}

/**
 * One tile in the sources gallery. Mirrors the CompilationCard pattern:
 * 16:9 thumbnail + filename + game tag + relative date.
 *
 * Origin badge: distinguishes manually-imported (Outplayed recordings,
 * hand-placed MP4s) from downloaded-via-URL files. Drives the
 * delete-source eligibility (only `downloaded` files can be auto-
 * cleaned up — manually placed files are sacred).
 *
 * Click target: an internal route to the asset detail (not built yet)
 * OR a direct trigger of analyze/compile. For now, links go nowhere
 * (the tile is informational) — the click target will land alongside
 * the asset detail page in a follow-up.
 */
export function AssetCard({ asset, index }: AssetCardProps) {
  const qc = useQueryClient();
  // Track 404s so we can swap to a placeholder. Older assets indexed
  // before auto-thumbnail-extraction shipped will hit this path.
  const [thumbBroken, setThumbBroken] = useState(false);
  const thumbUrl = assetThumbnailUrl(asset.id);
  const showThumb = !thumbBroken;
  const shortId = asset.id.slice(0, 8);
  const isDownloaded = asset.source_origin === "downloaded";
  const isDeleted = !!asset.source_deleted_at;
  const showSplit = canSplit(asset);
  const durationText = formatDuration(asset.duration_seconds);

  // Split state: the mutation kicks off a background job; we poll the
  // job until completion and then refresh the assets list.
  const [splitJobId, setSplitJobId] = useState<string | null>(null);
  const [splitStatus, setSplitStatus] = useState<"idle" | "running" | "done" | "failed">("idle");
  const [splitMessage, setSplitMessage] = useState<string | null>(null);
  const pollTimer = useRef<number | null>(null);

  const split = useMutation({
    mutationFn: () => splitVodIntoGames(asset.id),
    onSuccess: (resp) => {
      setSplitJobId(resp.job_id);
      setSplitStatus("running");
      setSplitMessage("detecting game boundaries…");
    },
    onError: (err: Error) => {
      setSplitStatus("failed");
      setSplitMessage(err.message);
    },
  });

  useEffect(() => {
    if (splitStatus !== "running" || !splitJobId) return;
    let cancelled = false;
    async function poll() {
      if (!splitJobId) return;
      try {
        const job = await getJob(splitJobId);
        if (cancelled) return;
        if (job.status === "completed") {
          setSplitStatus("done");
          setSplitMessage(job.output_path ?? "split done");
          qc.invalidateQueries({ queryKey: ["assets"] });
          return;
        }
        if (job.status === "failed") {
          setSplitStatus("failed");
          setSplitMessage(job.error ?? "split failed");
          return;
        }
        pollTimer.current = window.setTimeout(poll, 4000);
      } catch (err) {
        if (cancelled) return;
        setSplitStatus("failed");
        setSplitMessage((err as Error).message);
      }
    }
    poll();
    return () => {
      cancelled = true;
      if (pollTimer.current) {
        window.clearTimeout(pollTimer.current);
        pollTimer.current = null;
      }
    };
  }, [splitStatus, splitJobId, qc]);

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
      <div className="surface-interactive hover:border-accent/40 group block overflow-hidden rounded-lg border border-border bg-bg-elevated transition-all hover:shadow-[0_0_0_1px_var(--accent-glow)]">
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
          {/* Origin / deleted-source overlays. Top-right so it's visible
           *  but doesn't fight the thumbnail's natural focal point. */}
          <div className="absolute right-2 top-2 flex flex-wrap items-center justify-end gap-1">
            {isDownloaded && <Badge tone="accent">downloaded</Badge>}
            {isDeleted && <Badge tone="danger">file deleted</Badge>}
          </div>
        </div>

        <div className="space-y-1.5 p-4">
          <div className="truncate text-sm text-text-primary" title={asset.filename}>
            {asset.filename}
          </div>
          <div className="flex flex-wrap items-center gap-2 font-mono text-[10px] uppercase tracking-wider text-text-dim">
            {asset.parent_asset_id && (
              <>
                <Badge tone="muted">split child</Badge>
                <span className="text-text-dim/50">·</span>
              </>
            )}
            {asset.game && (
              <>
                <span>{asset.game}</span>
                <span className="text-text-dim/50">·</span>
              </>
            )}
            {durationText && (
              <>
                <span>{durationText}</span>
                <span className="text-text-dim/50">·</span>
              </>
            )}
            <span>{relativeTime(asset.created_at)}</span>
            <span className="text-text-dim/50">·</span>
            <span className="text-text-muted">{shortId}</span>
          </div>

          {showSplit && (
            <div className="border-t border-border pt-3">
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-[10px] uppercase tracking-wider text-text-dim">
                  multi-game VOD?
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={splitStatus === "running" || splitStatus === "done"}
                  onClick={() => split.mutate()}
                >
                  {splitStatus === "running"
                    ? "splitting…"
                    : splitStatus === "done"
                      ? "split done"
                      : "split into games"}
                </Button>
              </div>
              {splitMessage && (
                <div
                  className={
                    splitStatus === "failed"
                      ? "border-danger/40 bg-danger/10 mt-2 rounded border p-2 font-mono text-[10px] text-danger"
                      : splitStatus === "done"
                        ? "border-success/40 bg-success/10 mt-2 rounded border p-2 font-mono text-[10px] text-success"
                        : "mt-2 rounded border border-border bg-bg-base p-2 font-mono text-[10px] text-text-muted"
                  }
                >
                  {splitMessage}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </motion.div>
  );
}
