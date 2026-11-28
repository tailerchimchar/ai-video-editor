import { memo } from "react";
import { pxToS, sToPx, type Viewport } from "@/lib/timeline";
import type { FrameStrip } from "@/types/narration";
import { LANE_HEIGHT, laneX } from "./lane";

interface FrameStripLaneProps {
  view: Viewport;
  frames: FrameStrip | undefined;
  /** Draw every frame this much later (the video after a re-planned hook slides). */
  shiftS?: number;
  onSeek: (s: number) => void;
}

/**
 * Video frames from the API's sprite sheet. Zoomed out, only every n-th
 * frame is drawn so each keeps its width; zoomed in, a frame repeats
 * across the span it covers. Only the frames in view are rendered.
 */
export const FrameStripLane = memo(function FrameStripLane({
  view,
  frames,
  shiftS = 0,
  onSeek,
}: FrameStripLaneProps) {
  const height = LANE_HEIGHT.frames;
  if (!frames || frames.count === 0) {
    return (
      <div
        className="flex items-center px-2 font-mono text-[10px] text-text-dim"
        style={{ height }}
      >
        rendering frames…
      </div>
    );
  }

  const tileW = (frames.tile_w * height) / frames.tile_h;
  const slotW = frames.every_s * view.pxPerS;
  const stride = Math.max(1, Math.ceil(tileW / slotW));
  const rows = Math.ceil(frames.count / frames.cols);
  const first = Math.floor((view.scrollS - shiftS) / frames.every_s / stride) * stride;
  const last = Math.min(
    frames.count - 1,
    Math.ceil((pxToS(view, view.widthPx) - shiftS) / frames.every_s),
  );
  // Zoomed in, a frame repeats across its span instead of leaving a gap.
  const copies = Math.max(1, Math.ceil((stride * slotW) / tileW));
  const tiles: number[] = [];
  for (let i = Math.max(0, first); i <= last; i += stride) tiles.push(i);

  return (
    <div
      className="relative cursor-pointer overflow-hidden bg-bg-base"
      style={{ height }}
      onPointerDown={(e) => onSeek(Math.max(0, pxToS(view, laneX(e, e.currentTarget)) - shiftS))}
    >
      {tiles.map((i) => (
        <div
          key={i}
          className="absolute top-0 overflow-hidden border-r border-bg-base"
          style={{ left: sToPx(view, i * frames.every_s + shiftS), width: stride * slotW, height }}
        >
          {Array.from({ length: copies }, (_, c) => (
            <div
              key={c}
              className="absolute top-0"
              style={{
                left: c * tileW,
                width: tileW,
                height,
                backgroundImage: `url(${frames.sprite_url})`,
                backgroundSize: `${frames.cols * tileW}px ${rows * height}px`,
                backgroundPosition: `-${(i % frames.cols) * tileW}px -${Math.floor(i / frames.cols) * height}px`,
              }}
            />
          ))}
        </div>
      ))}
    </div>
  );
});
