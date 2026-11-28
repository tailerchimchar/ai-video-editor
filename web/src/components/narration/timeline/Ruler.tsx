import { useRef, type PointerEvent } from "react";
import { mmss } from "@/lib/time";
import { pxToS, sToPx, tickEvery, type Viewport } from "@/lib/timeline";
import { LANE_HEIGHT, laneX } from "./lane";

interface RulerProps {
  view: Viewport;
  durationS: number;
  onSeek: (s: number) => void;
}

/** Time ruler: a labelled tick every `tickEvery` seconds, minor ticks between. Drag to scrub. */
export function Ruler({ view, durationS, onSeek }: RulerProps) {
  const scrubbing = useRef(false);
  const step = tickEvery(view.pxPerS);
  const minor = step / (step % 5 === 0 ? 5 : 2);
  const endS = Math.min(durationS, pxToS(view, view.widthPx));
  const ticks: { s: number; major: boolean }[] = [];
  for (let i = Math.ceil(view.scrollS / minor); i * minor <= endS; i++) {
    const s = i * minor;
    ticks.push({ s, major: Math.abs(s / step - Math.round(s / step)) < 1e-6 });
  }

  const seekAt = (e: PointerEvent<HTMLDivElement>) =>
    onSeek(Math.min(durationS, Math.max(0, pxToS(view, laneX(e, e.currentTarget)))));
  const stop = () => {
    scrubbing.current = false;
  };

  return (
    <div
      className="relative cursor-pointer select-none overflow-hidden border-b border-border"
      style={{ height: LANE_HEIGHT.ruler }}
      onPointerDown={(e) => {
        scrubbing.current = true;
        e.currentTarget.setPointerCapture(e.pointerId);
        seekAt(e);
      }}
      onPointerMove={(e) => {
        if (scrubbing.current) seekAt(e);
      }}
      onPointerUp={stop}
      onPointerCancel={stop}
    >
      {ticks.map(({ s, major }) => (
        <div
          key={s}
          className={
            major
              ? "absolute bottom-0 h-2.5 w-px bg-border-strong"
              : "absolute bottom-0 h-1 w-px bg-border"
          }
          style={{ left: sToPx(view, s) }}
        >
          {major && (
            <span className="absolute bottom-2 left-1 font-mono text-[10px] text-text-dim">
              {mmss(s)}
            </span>
          )}
        </div>
      ))}
    </div>
  );
}
