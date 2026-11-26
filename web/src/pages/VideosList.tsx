import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { listVideos, VIDEOS_QUERY_KEY } from "@/api/videos";
import { GalleryTabs } from "@/components/GalleryTabs";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { PageHeader } from "@/components/ui/PageHeader";
import { PageShell } from "@/components/ui/PageShell";
import { mmss } from "@/lib/time";
import type { VideoSummary } from "@/types/videos";

/**
 * `/videos` — every video you can narrate, across all kinds and games,
 * newest first within a kind. Each card links to its narration page.
 */
export function VideosList() {
  const { data, isPending, error } = useQuery({
    queryKey: VIDEOS_QUERY_KEY,
    queryFn: listVideos,
  });

  return (
    <PageShell>
      <PageHeader
        title="Videos"
        subtitle={
          isPending
            ? "loading…"
            : data
              ? `${data.length} ${data.length === 1 ? "video" : "videos"}`
              : ""
        }
        trailing={<GalleryTabs />}
      />

      {error && (
        <div className="surface-elevated rounded p-6 font-mono text-sm text-danger">
          failed to load videos · {(error as Error).message}
        </div>
      )}

      {data && data.length === 0 && (
        <div className="surface-elevated rounded p-6 font-mono text-sm text-text-muted">
          no videos yet · render a story round and it shows up here
        </div>
      )}

      {data && data.length > 0 && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {data.map((v) => (
            <VideoCard key={`${v.kind}/${v.key}`} video={v} />
          ))}
        </div>
      )}
    </PageShell>
  );
}

function VideoCard({ video: v }: { video: VideoSummary }) {
  return (
    <div className="surface-elevated flex flex-col gap-3 rounded-lg border border-border p-4">
      <div>
        <h2 className="text-base font-medium text-text-primary">{v.title}</h2>
        <p className="font-mono text-[11px] text-text-muted">
          {v.game}
          {v.date ? ` · ${v.date}` : ""}
          {v.duration_s != null ? ` · ${mmss(v.duration_s)}` : ""}
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="muted">
          {v.take_count} {v.take_count === 1 ? "take" : "takes"}
        </Badge>
        {v.exported && <Badge tone="success">exported ✓</Badge>}
        {v.stale && <Badge tone="danger">stale</Badge>}
        {v.suggestion_status === "ready" && <Badge tone="accent">suggestions ready</Badge>}
      </div>
      <Link
        to={`/narrate/${encodeURIComponent(v.kind)}/${encodeURIComponent(v.key)}`}
        className="mt-auto self-start"
      >
        <Button variant="ghost" size="sm">
          narrate
        </Button>
      </Link>
    </div>
  );
}
