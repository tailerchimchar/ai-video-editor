import type { MouseEvent } from "react";
import { mmss } from "@/lib/time";
import { cn } from "@/lib/cn";
import type { Take } from "@/types/narration";

interface TakeTimelineProps {
  takes: Take[];
  /** Video length; blocks are laid out as a share of it. */
  durationS: number;
  currentTimeS: number;
  onPlay: (take: Take) => void;
  onDelete: (takeId: string) => void;
  onSeek: (seconds: number) => void;
  /** Take currently being deleted (dims its block). */
  deletingId: string | null;
  disabled?: boolean;
}

/**
 * Takes as blocks along the video's duration, with a playhead.
 * Click a block = seek there and play that take's audio; click the
 * empty track = seek. A list below gives each take a delete button.
 */
export function TakeTimeline({
  takes,
  durationS,
  currentTimeS,
  onPlay,
  onDelete,
  onSeek,
  deletingId,
  disabled,
}: TakeTimelineProps) {
  const pct = (s: number) => (durationS > 0 ? `${(s / durationS) * 100}%` : "0%");
  const sorted = [...takes].sort((a, b) => a.start_s - b.start_s);

  function seekFromTrack(e: MouseEvent<HTMLDivElement>) {
    if (disabled || durationS <= 0) return;
    const rect = e.currentTarget.getBoundingClientRect();
    onSeek(((e.clientX - rect.left) / rect.width) * durationS);
  }

  return (
    <section className="surface-elevated space-y-3 rounded-lg p-4">
      <header className="flex items-baseline justify-between font-mono text-xs text-text-muted">
        <span>takes · {takes.length}</span>
        <span>
          {mmss(currentTimeS)} / {mmss(durationS)}
        </span>
      </header>

      <div
        className="relative h-10 cursor-pointer overflow-hidden rounded bg-bg-overlay"
        onClick={seekFromTrack}
      >
        {sorted.map((take) => (
          <button
            key={take.id}
            type="button"
            title={`${mmss(take.start_s)} – ${mmss(take.start_s + take.duration_s)}`}
            disabled={disabled}
            onClick={(e) => {
              e.stopPropagation();
              onPlay(take);
            }}
            className={cn(
              "absolute inset-y-1 rounded-sm border border-accent bg-accent-glow opacity-80 hover:opacity-100",
              take.has_camera && "border-success",
              deletingId === take.id && "opacity-30",
            )}
            style={{ left: pct(take.start_s), width: pct(take.duration_s) }}
          />
        ))}
        <div
          className="pointer-events-none absolute inset-y-0 w-px bg-text-primary"
          style={{ left: pct(currentTimeS) }}
        />
      </div>

      {sorted.length > 0 && (
        <ul className="space-y-1 font-mono text-xs">
          {sorted.map((take, i) => (
            <li key={take.id} className="flex items-center gap-3 text-text-muted">
              <span className="w-6 text-text-dim">{i + 1}</span>
              <button
                type="button"
                className="hover:text-text-primary"
                disabled={disabled}
                onClick={() => onPlay(take)}
              >
                ▶ {mmss(take.start_s)} · {take.duration_s.toFixed(1)}s
              </button>
              {take.has_camera && <span className="text-success">camera</span>}
              <button
                type="button"
                className="ml-auto text-danger hover:underline disabled:opacity-50"
                disabled={disabled || deletingId === take.id}
                onClick={() => onDelete(take.id)}
              >
                delete
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
