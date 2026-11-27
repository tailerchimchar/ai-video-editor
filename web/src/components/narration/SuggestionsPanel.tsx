import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { suggestionsKey } from "@/api/suggestions";
import { VIDEOS_QUERY_KEY } from "@/api/videos";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { useJob } from "@/hooks/useJob";
import { useSuggestions } from "@/hooks/useSuggestions";
import type { TargetRef } from "@/types/narration";
import type { SuggestionItem, SuggestionKind, SuggestionSetStatus } from "@/types/suggestions";

const KIND_LABEL: Record<SuggestionKind, string> = {
  letterbox_focus: "letterbox + focus",
  slowmo: "slow-mo",
  zoom: "zoom",
};

const STATUS_TONE: Record<SuggestionSetStatus, "muted" | "accent" | "success" | "danger"> = {
  pending: "muted",
  ready: "accent",
  applied: "success",
  failed: "danger",
};

/** Game-clock seconds as m:ss (what the HUD shows). */
function gameClock(s: number): string {
  const total = Math.max(0, Math.round(s));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/**
 * "Get editor suggestions" for a story video: the panel's 3-6 picks with a
 * checkbox each, and "Apply & re-render" for the ticked ones. Story only
 * (the round is the target key); other kinds render nothing.
 *
 * "Jump there" seeks the player to the suggestion's `video_s` (sent by the
 * API for the render the suggestions came from; hidden once applied, when
 * the video has changed).
 */
interface SuggestionsPanelProps {
  target: TargetRef;
  onSeek: (seconds: number) => void;
}

export function SuggestionsPanel({ target, onSeek }: SuggestionsPanelProps) {
  if (target.kind !== "story") return null;
  return <StorySuggestions target={target} onSeek={onSeek} />;
}

function StorySuggestions({ target, onSeek }: SuggestionsPanelProps) {
  const round = target.key;
  const qc = useQueryClient();
  const { set, start, tick, apply } = useSuggestions(round);
  const [applyJobId, setApplyJobId] = useState<string | null>(null);
  const applyJob = useJob(applyJobId);

  const data = set.data ?? null;
  const rendering = applyJob.data?.status === "pending" || applyJob.data?.status === "running";
  const renderDone = applyJob.data?.status === "completed";

  // When the re-render finishes the video changed: refetch the session (its
  // fingerprint moves, so earlier takes show the stale banner), the videos
  // list and the set (now `applied`).
  useEffect(() => {
    if (!renderDone) return;
    qc.invalidateQueries({ queryKey: ["narration", target.kind, target.key] });
    qc.invalidateQueries({ queryKey: VIDEOS_QUERY_KEY });
    qc.invalidateQueries({ queryKey: suggestionsKey(round) });
  }, [renderDone, qc, round, target.kind, target.key]);

  const pending = data?.status === "pending" || start.isPending;
  const items = data?.items ?? [];
  const ticked = items.filter((i) => i.accepted === true).length;
  const canApply = data?.status === "ready" && ticked > 0 && !apply.isPending && !rendering;
  const error =
    (set.error as Error | null)?.message ??
    start.error?.message ??
    tick.error?.message ??
    apply.error?.message ??
    null;

  return (
    <section className="surface-elevated space-y-4 rounded-lg p-4">
      <div className="flex items-center justify-between">
        <h2 className="font-mono text-xs uppercase tracking-wider text-text-muted">
          editor suggestions
        </h2>
        {data && <Badge tone={STATUS_TONE[data.status]}>{data.status}</Badge>}
      </div>

      {set.isLoading && <p className="font-mono text-xs text-text-muted">loading…</p>}

      {!set.isLoading && !data && (
        <p className="font-mono text-xs text-text-dim">
          Ask the panel which moments deserve slow-mo, zoom or a letterbox.
        </p>
      )}

      {pending && (
        <p className="font-mono text-xs text-text-muted">the panel is picking moments…</p>
      )}

      {data?.status === "failed" && (
        <p className="font-mono text-xs text-danger">
          suggestions failed · {data.error ?? "unknown error"}
        </p>
      )}

      {data?.status === "applied" && (
        <p className="font-mono text-xs text-text-muted">
          Applied and re-rendered. Takes recorded before may be stale.
        </p>
      )}

      {data && (data.status === "ready" || data.status === "applied") && items.length === 0 && (
        <p className="font-mono text-xs text-text-dim">no moments worth an effect this time</p>
      )}

      {items.length > 0 && data?.status !== "pending" && (
        <ul className="space-y-2">
          {items.map((item) => (
            <SuggestionRow
              key={item.id}
              item={item}
              disabled={data?.status !== "ready" || rendering || apply.isPending}
              onTick={(accepted) => tick.mutate({ itemId: item.id, accepted })}
              onJump={
                data?.status === "ready" && item.video_s !== null
                  ? () => onSeek(item.video_s as number)
                  : undefined
              }
            />
          ))}
        </ul>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <Button
          variant={data ? "ghost" : "primary"}
          size="sm"
          disabled={pending || rendering}
          onClick={() => start.mutate()}
        >
          {data ? "get new suggestions" : "get editor suggestions"}
        </Button>
        {data?.status === "ready" && (
          <Button
            variant="primary"
            size="sm"
            disabled={!canApply}
            onClick={() =>
              apply.mutate(undefined, { onSuccess: ({ job_id }) => setApplyJobId(job_id) })
            }
          >
            apply &amp; re-render ({ticked})
          </Button>
        )}
        {rendering && (
          <span className="font-mono text-xs text-text-muted">
            re-rendering · {applyJob.data?.status}…
          </span>
        )}
      </div>

      {applyJob.data?.status === "failed" && (
        <p className="font-mono text-xs text-danger">
          re-render failed · {applyJob.data.error ?? "unknown error"}
        </p>
      )}
      {renderDone && (
        <p className="font-mono text-xs text-success">re-rendered · the video above is updated</p>
      )}
      {error && <p className="font-mono text-xs text-danger">{error}</p>}
    </section>
  );
}

function SuggestionRow({
  item,
  disabled,
  onTick,
  onJump,
}: {
  item: SuggestionItem;
  disabled: boolean;
  onTick: (accepted: boolean) => void;
  /** Absent when the suggestion can't be placed in the current video. */
  onJump?: () => void;
}) {
  return (
    <li className="rounded border border-border bg-bg-overlay p-2">
      <label className="flex cursor-pointer items-start gap-2">
        <input
          type="checkbox"
          className="mt-0.5"
          checked={item.accepted === true}
          disabled={disabled}
          onChange={(e) => onTick(e.target.checked)}
        />
        <span className="min-w-0 flex-1 space-y-1">
          <span className="flex flex-wrap items-center gap-2">
            <Badge tone="accent">{KIND_LABEL[item.kind]}</Badge>
            <span className="font-mono text-xs text-text-primary">
              {gameClock(item.start_s)}–{gameClock(item.end_s)}
            </span>
            <span className="font-mono text-xs text-text-dim">
              chapter {item.chapter_index + 1}
            </span>
          </span>
          <span className="block font-mono text-xs text-text-muted">{item.reason}</span>
          <span className="flex gap-3 font-mono text-[10px] text-text-dim">
            {item.play_score !== null && <span>play {item.play_score}/5</span>}
            {item.pop_p !== null && <span>pop {Math.round(item.pop_p * 100)}%</span>}
            <span>{item.backend}</span>
          </span>
        </span>
      </label>
      {onJump && (
        <div className="mt-1 pl-6">
          <Button size="sm" variant="ghost" onClick={onJump}>
            jump there ↗
          </Button>
        </div>
      )}
    </li>
  );
}
