import { baseOf, type TrackLayout } from "@/lib/hookTrack";
import { clock, mmss } from "@/lib/time";
import type { HookPickView, HookView } from "@/types/narration";

interface HookPreviewLineProps {
  lay: TrackLayout;
  hook: HookView;
  extra: HookPickView[];
  /** Length of the mp4 as rendered. */
  videoS: number;
  /** The video's playhead, and where it is on the hook track (null = not on it). */
  playheadS: number;
  trackS: number | null;
}

const signed = (x: number) => `${x >= 0 ? "+" : "−"}${Math.abs(x).toFixed(1)}`;

/**
 * One line above the timeline: the intro and video length now → after the
 * laptop re-joins, and what the playhead is playing.
 */
export function HookPreviewLine({
  lay,
  hook,
  extra,
  videoS,
  playheadS,
  trackS,
}: HookPreviewLineProps) {
  const after = videoS + (lay.introAfter - hook.rendered_intro_s);
  let plays = "";
  if (playheadS >= hook.rendered_intro_s) {
    plays = `plays the video at ${clock(playheadS)}${Math.abs(lay.shift) >= 0.05 ? ` (slid ${signed(lay.shift)} s)` : ""}`;
  } else if (trackS !== null) {
    const h = lay.hooks.find((x) => trackS >= x.vs && trackS < x.ve);
    if (h) {
      const b = baseOf(hook, h.clip.chapter_id, extra);
      const game = b.base_rec_start_s - h.clip.start_s + (trackS - h.vs) - b.offset_s;
      plays = `plays hook clip ${h.n + 1} at game ${clock(game)}`;
    }
  }
  return (
    <p className="flex flex-wrap gap-x-3 font-mono text-xs text-text-dim">
      <span>
        intro <span className="text-text-primary">{hook.rendered_intro_s.toFixed(1)}</span> →{" "}
        <span className="text-text-primary">{lay.introAfter.toFixed(1)} s</span>
        {lay.emptyS >= 0.05 && (
          <span className="text-warn"> ({lay.emptyS.toFixed(1)} s of empty space here)</span>
        )}
      </span>
      <span>·</span>
      <span>
        video <span className="text-text-primary">{mmss(videoS)}</span> →{" "}
        <span className="text-text-primary">{mmss(after)}</span>
      </span>
      {plays && (
        <>
          <span>·</span>
          <span>{plays}</span>
        </>
      )}
    </p>
  );
}
