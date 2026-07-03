import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ClipActionsPanel } from "@/components/ClipActionsPanel";
import { ClipFilmstrip } from "@/components/ClipFilmstrip";
import { ClipMetaPanel } from "@/components/ClipMetaPanel";
import { DeleteSourcePanel } from "@/components/DeleteSourcePanel";
import { HistoryRail } from "@/components/HistoryRail";
import { VLMReviewPanel } from "@/components/VLMReviewPanel";
import { VideoPlayer } from "@/components/VideoPlayer";
import { Button } from "@/components/ui/Button";
import { PageHeader } from "@/components/ui/PageHeader";
import { PageShell } from "@/components/ui/PageShell";
import { useCompilation } from "@/hooks/useCompilation";
import { parseClipRef } from "@/lib/time";
import { parseCompilationTitle } from "@/lib/title";

const parseMmss = parseClipRef;

/**
 * Phase 1.5 hero screen — video + filmstrip + detail panel + history.
 *
 * Layout:
 *   ┌───────────────────────────────────────┬───────────────────┐
 *   │  hero video                           │  history rail     │
 *   ├───────────────────────────────────────┤                   │
 *   │  filmstrip of clip tiles (horizontal) │                   │
 *   ├───────────────────────────────────────┤                   │
 *   │  detail panel for the selected clip   │                   │
 *   └───────────────────────────────────────┴───────────────────┘
 *
 * Selection state lives in the URL hash (`#clip=<uuid>`) so refreshing
 * the page restores the focus. Cleared automatically when the clip is
 * removed (e.g. via revert).
 */
export function CompilationViewer() {
  const { id } = useParams<{ id: string }>();
  if (!id) return null;

  const {
    metadata,
    clips,
    history,
    extend,
    revert,
    editCaptions,
    addCaption,
    removeCaption,
    tiktokify,
    addZoom,
    addFocus,
    reorder,
    vlmHealth,
    vlmReview,
  } = useCompilation(id);

  const videoUrl = buildWorkspaceUrl(metadata.data?.output_path);
  const thumbnailUrl = metadata.data?.output_path
    ? buildWorkspaceUrl(metadata.data.output_path.replace(/compilation\.mp4$/, "thumbnail.jpg"))
    : null;
  const thumbnailsBaseUrl = metadata.data?.output_path
    ? buildWorkspaceFolderUrl(metadata.data.output_path.replace(/compilation\.mp4$/, "_thumbnails"))
    : null;

  // Bumps with the journal length so the video AND clip thumbnails
  // re-fetch after every successful edit (server overwrites both).
  const videoVersion = history.data?.history.length ?? 0;

  const stem = metadata.data?.output_path ? folderStem(metadata.data.output_path) : null;
  const title = parseCompilationTitle(stem);

  const subtitleParts = [
    title.gameDate && `played ${title.gameDate}`,
    title.compileTime && `rendered ${title.compileTime}`,
    `id ${id.slice(0, 8)}`,
  ].filter(Boolean);

  // Selection state — synced with URL hash so reloads + back/forward
  // remember the focused clip. Default: first clip (whatever it is).
  const [selectedId, setSelectedId] = useState<string | null>(() => readClipHash());

  useEffect(() => {
    function onHashChange() {
      setSelectedId(readClipHash());
    }
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  // Default selection to the first clip the moment data lands.
  useEffect(() => {
    if (selectedId) return;
    const first = clips.data?.clips[0];
    if (first) {
      setSelectedId(first.id);
      // Don't write to hash here — keep navigation history clean; user
      // gets a hash entry only after they click a tile themselves.
    }
  }, [clips.data, selectedId]);

  // If the selected clip vanishes (revert removed it), fall back to
  // the first clip so the panel doesn't show stale data.
  useEffect(() => {
    if (!clips.data) return;
    const stillThere = clips.data.clips.some((c) => c.id === selectedId);
    if (!stillThere) {
      const first = clips.data.clips[0];
      setSelectedId(first ? first.id : null);
    }
  }, [clips.data, selectedId]);

  // Imperative seek on tile click — we want the video to jump to the
  // clip's reel start. Keep playback STATE (playing/paused) intact:
  // setting `currentTime` mid-playback simply seeks without pausing.
  const videoRef = useRef<HTMLVideoElement>(null);

  // Track the video's current reel-time so we can derive which clip is
  // playing and pass it down to the filmstrip. Updates ~4 Hz from the
  // <video> element's timeupdate event. Cheap to keep in state.
  const [currentReelTime, setCurrentReelTime] = useState<number>(0);

  function selectClip(clipId: string) {
    setSelectedId(clipId);
    window.location.hash = `clip=${clipId}`;
    // Find the clip's reel start by summing preceding durations.
    // Doing it inline avoids stale closures over the useMemo positions.
    const allClips = clips.data?.clips ?? [];
    let running = 0;
    for (const c of allClips) {
      if (c.id === clipId) {
        if (videoRef.current) {
          videoRef.current.currentTime = running;
        }
        return;
      }
      running += c.duration;
    }
  }

  // Derive which clip is currently playing based on the video's
  // reel-time. Walking the cumulative durations is O(N) and fine
  // for typical reels (~5-20 clips). Returns null only when there
  // are no clips at all.
  const playingClipId: string | null = useMemo(() => {
    const allClips = clips.data?.clips ?? [];
    let running = 0;
    for (const c of allClips) {
      const end = running + c.duration;
      if (currentReelTime < end) return c.id;
      running = end;
    }
    // Past the last clip's end (paused at the very end) — highlight
    // the last clip so the strip doesn't go dark.
    return allClips.length > 0 ? (allClips[allClips.length - 1]?.id ?? null) : null;
  }, [clips.data, currentReelTime]);

  const selectedClip = clips.data?.clips.find((c) => c.id === selectedId) ?? null;

  return (
    <PageShell>
      <PageHeader
        title={title.game || `Compilation · ${id.slice(0, 8)}`}
        subtitle={subtitleParts.join("  ·  ")}
        trailing={
          <Link to="/">
            <Button variant="ghost" size="sm">
              ← all compilations
            </Button>
          </Link>
        }
      />

      {/* Flat layout — VideoPlayer + ClipFilmstrip + HistoryRail stay
       *  mounted regardless of selection state so seeking, scroll, and
       *  history don't get torn down between renders. Only the clip
       *  detail panels are conditional. */}
      {(() => {
        const sel = selectedClip;
        const [srcStartStr, srcEndStr] = sel
          ? sel.source.split("-").map((s) => s.trim())
          : [undefined, undefined];
        const clipSourceStart = (srcStartStr && parseMmss(srcStartStr)) || 0;
        const clipSourceEnd =
          (srcEndStr && parseMmss(srcEndStr)) || clipSourceStart + (sel?.duration ?? 0);
        const clipRef = sel ? String(sel.index) : "";
        const captionsBusy = !!(
          (editCaptions.isPending && editCaptions.variables?.clipRef === clipRef) ||
          (addCaption.isPending && addCaption.variables?.clipRef === clipRef) ||
          (removeCaption.isPending && removeCaption.variables?.clipRef === clipRef) ||
          (tiktokify.isPending && tiktokify.variables === clipRef)
        );
        const extendBusy = !!(extend.isPending && extend.variables?.clipRef === clipRef);
        const effectsBusy = !!(
          (addZoom.isPending && addZoom.variables?.clipRef === clipRef) ||
          (addFocus.isPending && addFocus.variables?.clipRef === clipRef)
        );

        return (
          <div className="grid grid-cols-1 gap-8 lg:grid-cols-12">
            {/* LEFT — video + filmstrip + (selected) clip metadata */}
            <div className="space-y-6 lg:col-span-7">
              {videoUrl ? (
                <VideoPlayer
                  ref={videoRef}
                  src={videoUrl}
                  poster={thumbnailUrl ?? undefined}
                  version={videoVersion}
                  onTimeUpdate={setCurrentReelTime}
                />
              ) : (
                <div className="surface-elevated flex aspect-video items-center justify-center rounded-lg font-mono text-sm text-text-muted">
                  no rendered output yet
                </div>
              )}

              <ClipFilmstrip
                clips={clips.data?.clips ?? []}
                selectedId={selectedId}
                playingId={playingClipId}
                thumbnailsBaseUrl={thumbnailsBaseUrl}
                cacheBust={videoVersion}
                busy={extend.isPending || reorder.isPending}
                onSelect={selectClip}
                onExtend={(clipId, deltas) => {
                  // Find the dropped clip's index → use it as clip_ref.
                  // The backend extend_clip mutator accepts numeric refs
                  // (1-based) or UUID prefixes; we use the index for
                  // consistency with how the rest of the UI talks to it.
                  const targetIdx = (clips.data?.clips ?? []).findIndex((c) => c.id === clipId);
                  if (targetIdx < 0) return;
                  extend.mutate({
                    clipRef: String(targetIdx + 1),
                    before: deltas.before,
                    after: deltas.after,
                  });
                }}
                onReorder={(newOrder) => reorder.mutate(newOrder)}
              />

              {sel && <ClipMetaPanel clip={sel} busy={extendBusy} />}
            </div>

            {/* RIGHT — caption editor + future actions + history */}
            <aside className="space-y-6 lg:col-span-5">
              {sel && (
                <ClipActionsPanel
                  clip={sel}
                  clipSourceStart={clipSourceStart}
                  clipSourceEnd={clipSourceEnd}
                  captionsBusy={captionsBusy}
                  effectsBusy={effectsBusy}
                  onEditCaptions={(newSegments) =>
                    editCaptions.mutate({ clipRef, segments: newSegments })
                  }
                  onAddCaption={({ startSeconds, endSeconds, text }) =>
                    addCaption.mutate({ clipRef, startSeconds, endSeconds, text })
                  }
                  onRemoveCaption={(segmentIndex) =>
                    removeCaption.mutate({ clipRef, segmentIndex })
                  }
                  onTiktokify={() => tiktokify.mutate(clipRef)}
                  onAddZoom={({ roi, factor }) => addZoom.mutate({ clipRef, roi, factor })}
                  onAddFocus={({ x, y, radius, dim }) =>
                    addFocus.mutate({ clipRef, x, y, radius, dim })
                  }
                />
              )}

              <HistoryRail
                entries={history.data?.history ?? []}
                reverting={revert.isPending}
                onRevert={(toVersion) => revert.mutate({ to_version: toVersion })}
              />

              <VLMReviewPanel
                health={vlmHealth.data}
                healthLoading={vlmHealth.isLoading}
                review={vlmReview.data}
                reviewPending={vlmReview.isPending}
                reviewError={vlmReview.error as Error | null}
                onReview={() => vlmReview.mutate()}
              />

              <DeleteSourcePanel assetId={metadata.data?.asset_id} />

              {extend.isError && (
                <div className="border-danger/40 bg-danger/10 rounded border p-3 font-mono text-xs text-danger">
                  extend failed · {(extend.error as Error).message}
                </div>
              )}
              {revert.isError && (
                <div className="border-danger/40 bg-danger/10 rounded border p-3 font-mono text-xs text-danger">
                  revert failed · {(revert.error as Error).message}
                </div>
              )}
              {editCaptions.isError && (
                <div className="border-danger/40 bg-danger/10 rounded border p-3 font-mono text-xs text-danger">
                  caption edit failed · {(editCaptions.error as Error).message}
                </div>
              )}
              {addZoom.isError && (
                <div className="border-danger/40 bg-danger/10 rounded border p-3 font-mono text-xs text-danger">
                  zoom failed · {(addZoom.error as Error).message}
                </div>
              )}
              {addFocus.isError && (
                <div className="border-danger/40 bg-danger/10 rounded border p-3 font-mono text-xs text-danger">
                  focus failed · {(addFocus.error as Error).message}
                </div>
              )}
            </aside>
          </div>
        );
      })()}
    </PageShell>
  );
}

/** Read `#clip=<uuid>` out of the URL hash; null when absent. */
function readClipHash(): string | null {
  if (typeof window === "undefined") return null;
  const match = window.location.hash.match(/clip=([^&]+)/);
  return match ? decodeURIComponent(match[1] ?? "") : null;
}

/**
 * Map the API's filesystem `output_path` to a URL the browser can load
 * through the Vite proxy. Slice off everything before `/compilations/`
 * so any user's workspace root works.
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
 * Map a *folder* path (no trailing file segment) to a workspace URL.
 * Used for the `_thumbnails/` directory the filmstrip pulls per-clip
 * images from.
 */
function buildWorkspaceFolderUrl(folderPath: string | null | undefined): string | null {
  if (!folderPath) return null;
  const normalised = folderPath.replace(/\\/g, "/").replace(/\/$/, "");
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

function folderStem(outputPath: string): string {
  const normalised = outputPath.replace(/\\/g, "/");
  const parts = normalised.split("/");
  const idx = parts.lastIndexOf("compilation.mp4");
  if (idx > 0) {
    const parent = parts[idx - 1];
    if (parent) return parent;
  }
  return outputPath;
}
