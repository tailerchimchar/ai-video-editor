import * as Slider from "@radix-ui/react-slider";
import { useEffect, useState } from "react";
import { Button } from "./ui/Button";
import { formatDuration, mmss } from "@/lib/time";
import { cn } from "@/lib/cn";

interface ReelSegment {
  /** Clip id — used as the React key for the background segments. */
  id: string;
  reelStart: number;
  reelEnd: number;
  /** True when this is the same clip as the slider's active clip. */
  isCurrent: boolean;
}

interface ExtendSliderProps {
  /** This clip's reel position (seconds). */
  clipReelStart: number;
  clipReelEnd: number;
  /** Sum of all clip durations — defines the slider's max axis value. */
  totalReelSeconds: number;
  /** Every clip's reel position, including this one, drawn as background segments. */
  segments: ReelSegment[];
  /** How far past the current reel duration the user may extend (seconds). */
  maxExpand?: number;
  /** Disable interactions during in-flight mutations. */
  disabled?: boolean;
  /** Fired on commit. `before`/`after` are SOURCE-space deltas (same semantics as the API). */
  onCommit: (deltas: { before: number; after: number }) => void;
}

/**
 * Reel-timeline range slider for one clip.
 *
 * Each clip's slider shares the SAME axis — the entire reel timeline,
 * from 0 to (total reel duration + buffer). The clip's active range
 * marks where IT sits on the timeline. Background ticks render the
 * OTHER clips as faded segments for context.
 *
 * Dragging:
 *  - LEFT handle LEFT  → extend pre-roll (adds source content before; `before > 0`).
 *    The clip's reel_start in the next render WILL stay where it is
 *    (preceding clip's reel_end), but the clip's DURATION grows so
 *    its reel_end shifts right and downstream clips slide right.
 *  - LEFT handle RIGHT → trim pre-roll (`before < 0`).
 *  - RIGHT handle RIGHT → extend post-roll (`after > 0`).
 *  - RIGHT handle LEFT  → trim post-roll (`after < 0`).
 *
 * The visual axis is reel-time. The deltas we send to the backend
 * happen to map 1:1 to source seconds because extending the source
 * window by N seconds extends the reel by N seconds for that clip.
 *
 * Both commit paths (release the handle, or click "apply") fire the
 * same commit handler. Either feels native to the user.
 */
export function ExtendSlider({
  clipReelStart,
  clipReelEnd,
  totalReelSeconds,
  segments,
  maxExpand = 30,
  disabled,
  onCommit,
}: ExtendSliderProps) {
  // Axis covers the whole reel plus a buffer at the END for extending
  // the LAST clip rightward. The LEFT side starts at 0 (the reel's
  // own start) because no clip can extend before time 0 in the reel.
  const axisMin = 0;
  const axisMax = totalReelSeconds + maxExpand;
  const axisSpan = Math.max(1, axisMax - axisMin);

  const [value, setValue] = useState<[number, number]>([clipReelStart, clipReelEnd]);

  // Sync local state when the API returns new positions (post-commit
  // success) so the handles snap to the new resting position.
  useEffect(() => {
    setValue([clipReelStart, clipReelEnd]);
  }, [clipReelStart, clipReelEnd]);

  const [draggedStart, draggedEnd] = value;
  const beforeDelta = clipReelStart - draggedStart; // dragging left handle LEFT → positive `before`
  const afterDelta = draggedEnd - clipReelEnd; // dragging right handle RIGHT → positive `after`
  const liveDuration = Math.max(0, draggedEnd - draggedStart);
  const dirty = beforeDelta !== 0 || afterDelta !== 0;

  function applyChanges() {
    if (!dirty) return;
    const before = Math.round(beforeDelta * 100) / 100;
    const after = Math.round(afterDelta * 100) / 100;
    onCommit({ before, after });
  }

  // Render background segments as percentage offsets along the axis.
  function pct(seconds: number): number {
    return ((seconds - axisMin) / axisSpan) * 100;
  }

  return (
    <div className="space-y-3">
      {/* Top row — live position + duration + apply button */}
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-baseline gap-3">
          <span
            className={cn(
              "font-mono text-sm transition-colors",
              dirty ? "text-accent" : "text-text-primary",
            )}
          >
            {mmss(draggedStart)} → {mmss(draggedEnd)}
          </span>
          <span className="font-mono text-xs text-text-muted">{formatDuration(liveDuration)}</span>
        </div>
        <Button
          size="sm"
          variant={dirty ? "primary" : "ghost"}
          disabled={!dirty || disabled}
          onClick={applyChanges}
        >
          {disabled ? "applying…" : dirty ? "apply" : "no change"}
        </Button>
      </div>

      {/* The slider. Track shows the whole reel; other clips render
       *  as background segments for spatial context. */}
      <div className="relative pt-3">
        {/* Axis labels at the start + midpoint + end of the visible reel. */}
        <div className="pointer-events-none absolute -top-1 left-0 right-0 flex justify-between font-mono text-[10px] text-text-dim">
          <span>0:00</span>
          <span>{mmss(totalReelSeconds / 2)}</span>
          <span>{mmss(totalReelSeconds)}</span>
        </div>

        <Slider.Root
          min={axisMin}
          max={axisMax}
          step={0.1}
          minStepsBetweenThumbs={5}
          value={value}
          onValueChange={(v) => setValue([v[0] ?? axisMin, v[1] ?? axisMax])}
          onValueCommit={applyChanges}
          disabled={disabled}
          className={cn(
            "relative flex h-7 w-full touch-none select-none items-center",
            disabled && "opacity-50",
          )}
        >
          <Slider.Track className="relative h-2 w-full rounded-full bg-border-strong">
            {/* The whole reel as background segments. Current clip is brighter so
             *  the user sees "this is the one I'm editing." Other clips are faint
             *  but visible so the reel layout reads at a glance. */}
            {segments.map((seg) => (
              <div
                key={seg.id}
                aria-hidden
                className={cn(
                  "absolute top-0 h-full rounded-sm",
                  seg.isCurrent ? "bg-accent/50" : "bg-text-dim/30",
                )}
                style={{
                  left: `${pct(seg.reelStart)}%`,
                  width: `${Math.max(0, pct(seg.reelEnd) - pct(seg.reelStart))}%`,
                }}
              />
            ))}
            {/* The user's live drag range — bright on top of everything. */}
            <Slider.Range className="absolute h-full rounded-full bg-accent" />
          </Slider.Track>
          <Thumb label="extend before / shrink from left" />
          <Thumb label="extend after / shrink from right" />
        </Slider.Root>
      </div>
    </div>
  );
}

function Thumb({ label }: { label: string }) {
  return (
    <Slider.Thumb
      aria-label={label}
      className={cn(
        "block h-5 w-5 rounded-full border-2 border-accent bg-text-primary",
        "shadow-[0_0_0_4px_var(--accent-glow)]",
        "cursor-grab active:cursor-grabbing",
        "transition-transform hover:scale-110 active:scale-100",
        "focus-visible:shadow-[0_0_0_6px_var(--accent-glow)] focus-visible:outline-none",
      )}
    />
  );
}
