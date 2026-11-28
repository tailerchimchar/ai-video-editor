import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { useDebouncedDraft } from "@/hooks/useDebouncedDraft";
import { usePlayhead } from "@/hooks/usePlayhead";
import { MIN_TAKE_S, effectiveEnd, effectiveStart } from "@/lib/takeEdits";
import { mmss } from "@/lib/time";
import type { Take, TakeEdit } from "@/types/narration";
import { SliderRow } from "./SliderRow";

interface TakeInspectorProps {
  take: Take;
  /** The playhead is read here (every frame while playing), not in the page. */
  video: HTMLVideoElement | null;
  onEdit: (edit: TakeEdit) => void;
  /** Seek to the take's start and play. */
  onPlay: () => void;
  onSplit: (atS: number) => void;
  onDelete: () => void;
  /** Split or delete in flight. */
  busy: boolean;
}

const GAIN_SAVE_DELAY_MS = 300;

const decibels = (gain: number) => {
  const db = 20 * Math.log10(gain);
  return `${db >= 0 ? "+" : "−"}${Math.abs(db).toFixed(1)} dB`;
};

/**
 * The selected take: span, gain, mute, split at the playhead, its nudge
 * (with reset) and delete. Mount it keyed by the take id.
 */
export function TakeInspector({
  take,
  video,
  onEdit,
  onPlay,
  onSplit,
  onDelete,
  busy,
}: TakeInspectorProps) {
  const playheadS = usePlayhead(video);
  const [gain, setGain] = useDebouncedDraft(
    take.gain,
    (g) => onEdit({ gain: g }),
    GAIN_SAVE_DELAY_MS,
  );
  const start = effectiveStart(take);
  const end = effectiveEnd(take);
  // The API refuses a split closer than MIN_TAKE_S to either end.
  const canSplit = playheadS >= start + MIN_TAKE_S && playheadS <= end - MIN_TAKE_S;
  const offset = take.offset_s;

  return (
    <section className="surface-elevated space-y-4 rounded-lg p-4">
      <header className="flex items-center gap-2">
        <h2 className="font-mono text-xs uppercase tracking-wider text-text-muted">take</h2>
        <span className="font-mono text-xs text-text-primary">
          {mmss(start)} → {mmss(end)}
        </span>
        <span className="font-mono text-xs text-text-dim">{take.duration_s.toFixed(1)} s</span>
        <span className="ml-auto flex gap-1">
          {take.has_camera && <Badge tone="success">camera</Badge>}
          {take.muted && <Badge tone="muted">muted</Badge>}
        </span>
      </header>

      <SliderRow
        label="gain"
        value={gain}
        min={0.05}
        max={2}
        step={0.05}
        format={decibels}
        onChange={setGain}
      />

      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" disabled={!video} onClick={onPlay}>
          play from here
        </Button>
        <Button
          size="sm"
          variant={take.muted ? "primary" : "ghost"}
          onClick={() => onEdit({ muted: !take.muted })}
        >
          {take.muted ? "unmute" : "mute"}
        </Button>
        <Button
          size="sm"
          disabled={!canSplit || busy}
          title={canSplit ? undefined : "move the playhead inside the take"}
          onClick={() => onSplit(playheadS)}
        >
          split at playhead
        </Button>
        <Button size="sm" variant="danger" disabled={busy} onClick={onDelete} className="ml-auto">
          delete
        </Button>
      </div>

      <div className="flex items-center gap-3 font-mono text-xs text-text-muted">
        <span className="whitespace-nowrap">
          nudge{" "}
          <span className="text-text-primary">
            {offset >= 0 ? "+" : "−"}
            {Math.abs(offset).toFixed(2)} s
          </span>
        </span>
        <Button size="sm" disabled={offset === 0} onClick={() => onEdit({ offset_s: 0 })}>
          reset
        </Button>
      </div>
      <p className="font-mono text-xs text-text-dim">
        Drag the block to nudge it (±3 s), its edges to trim.
      </p>
    </section>
  );
}
