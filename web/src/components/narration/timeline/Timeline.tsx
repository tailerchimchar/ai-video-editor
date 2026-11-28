import { useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { usePlayhead } from "@/hooks/usePlayhead";
import { useVideoClock } from "@/hooks/useVideoClock";
import { cn } from "@/lib/cn";
import { mmss } from "@/lib/time";
import { clampScroll, fit, sToPx, zoomAround, type Viewport } from "@/lib/timeline";
import type {
  AudioMix,
  FrameStrip,
  GameAudioRegion,
  Marker,
  Peaks,
  Take,
  TakeEdit,
} from "@/types/narration";
import { FrameStripLane } from "./FrameStripLane";
import { GameAudioLane } from "./GameAudioLane";
import { LANE_HEIGHT } from "./lane";
import { MarkersLane } from "./MarkersLane";
import { Playhead } from "./Playhead";
import { Ruler } from "./Ruler";
import { TakesLane } from "./TakesLane";

interface TimelineProps {
  /** Video length; nothing is drawn until it is known. */
  durationS: number;
  /** The video the playhead follows (read every frame here, not in the page). */
  video: HTMLVideoElement | null;
  /** While recording: lanes ignore the pointer. */
  disabled?: boolean;
  onSeek: (s: number) => void;

  takes: Take[];
  selectedTakeId: string | null;
  onSelectTake: (takeId: string | null) => void;
  onPlayTake: (take: Take) => void;
  onTakeEdit: (takeId: string, edit: TakeEdit) => void;
  buffers: Map<string, AudioBuffer>;
  waveforms: Map<string, Float32Array>;

  frames: FrameStrip | undefined;
  peaks: Peaks | undefined;
  markers: Marker[];
  regions: GameAudioRegion[];
  mix: AudioMix;
  onRegionsChange: (regions: GameAudioRegion[]) => void;
}

const LANES: { label: string; height: number }[] = [
  { label: "", height: LANE_HEIGHT.ruler },
  { label: "frames", height: LANE_HEIGHT.frames },
  { label: "markers", height: LANE_HEIGHT.markers },
  { label: "takes", height: LANE_HEIGHT.takes },
  { label: "game", height: LANE_HEIGHT.game },
];
const ZOOM_STEP = 1.5;
/** Ctrl+wheel: zoom factor per pixel of wheel delta. */
const WHEEL_ZOOM = 0.0015;
const LINE_PX = 16;
/** While playing, scroll once the playhead passes this share of the width. */
const FOLLOW_AT = 0.9;

/**
 * The narration timeline: ruler, frames, markers, takes and game audio on
 * one zoomable time axis, with the playhead through all of them. Owns the
 * viewport (`null` zoom = fit the whole video); the lanes only draw it.
 * Wheel = pan, ctrl/⌘+wheel = zoom around the cursor.
 *
 * The playhead is read here every frame, so only this component and the
 * playhead re-render while playing: the lanes are memoised and their
 * canvases redraw only when the viewport or their data changes.
 */
export function Timeline(props: TimelineProps) {
  const { durationS } = props;
  const playheadS = usePlayhead(props.video);
  const { playing } = useVideoClock(props.video);
  const areaRef = useRef<HTMLDivElement>(null);
  const [widthPx, setWidthPx] = useState(0);
  const [zoom, setZoom] = useState<{ pxPerS: number; scrollS: number } | null>(null);

  // Memoised: a new object here would redraw every lane's canvas.
  const view: Viewport | null = useMemo(() => {
    if (widthPx <= 0 || durationS <= 0) return null;
    if (!zoom) return fit(widthPx, durationS);
    const pxPerS = Math.max(zoom.pxPerS, widthPx / durationS);
    return clampScroll({ pxPerS, scrollS: zoom.scrollS, widthPx }, durationS);
  }, [widthPx, durationS, zoom]);
  const viewRef = useRef(view);
  viewRef.current = view;
  const setView = (v: Viewport) => setZoom({ pxPerS: v.pxPerS, scrollS: v.scrollS });

  useEffect(() => {
    const el = areaRef.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setWidthPx(Math.floor(entry.contentRect.width));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // Native listener: React's wheel handler is passive and cannot stop the page scrolling.
  useEffect(() => {
    const el = areaRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      const v = viewRef.current;
      if (!v) return;
      e.preventDefault();
      const unit = e.deltaMode === 1 ? LINE_PX : 1;
      if (e.ctrlKey || e.metaKey) {
        const x = e.clientX - el.getBoundingClientRect().left;
        setView(zoomAround(v, Math.exp(-e.deltaY * unit * WHEEL_ZOOM), x, durationS));
      } else {
        const delta = (Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY) * unit;
        setView(clampScroll({ ...v, scrollS: v.scrollS + delta / v.pxPerS }, durationS));
      }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [durationS]);

  // Keep the playhead in view while playing (fit mode already shows it all).
  useEffect(() => {
    if (!playing || !view || !zoom) return;
    const x = sToPx(view, playheadS);
    if (x >= 0 && x <= view.widthPx * FOLLOW_AT) return;
    const visibleS = view.widthPx / view.pxPerS;
    const next = clampScroll(
      { ...view, scrollS: playheadS - visibleS * (1 - FOLLOW_AT) },
      durationS,
    );
    // Near the end the scroll is clamped: do not set the same view every frame.
    if (Math.abs(next.scrollS - view.scrollS) > 1e-6) setView(next);
    // Only the playhead moving should scroll; zoom / pan by the user must not.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playheadS, playing]);

  /** Zoom by the toolbar, around the playhead when it is in view, else the middle. */
  function zoomBy(factor: number) {
    if (!view) return;
    const x = sToPx(view, playheadS);
    const anchor = x >= 0 && x <= view.widthPx ? x : view.widthPx / 2;
    setView(zoomAround(view, factor, anchor, durationS));
  }

  const gameMarkers = useMemo(
    () => props.markers.filter((m) => m.kind === "game"),
    [props.markers],
  );

  return (
    <section className="surface-elevated space-y-3 rounded-lg p-4">
      <header className="flex items-center gap-3 font-mono text-xs text-text-muted">
        <span>takes · {props.takes.length}</span>
        <span className="text-text-dim">
          {mmss(playheadS)} / {mmss(durationS)}
        </span>
        <span className="ml-auto hidden text-text-dim md:inline">
          wheel = pan · ctrl+wheel = zoom
        </span>
        <div className="flex gap-1">
          <Button
            size="sm"
            aria-label="zoom out"
            disabled={!view}
            onClick={() => zoomBy(1 / ZOOM_STEP)}
          >
            −
          </Button>
          <Button size="sm" aria-label="zoom in" disabled={!view} onClick={() => zoomBy(ZOOM_STEP)}>
            +
          </Button>
          <Button size="sm" disabled={!zoom} onClick={() => setZoom(null)}>
            fit
          </Button>
        </div>
      </header>

      <div className="flex">
        <div className="w-16 shrink-0 font-mono text-[10px] uppercase tracking-wider text-text-dim">
          {LANES.map((lane, i) => (
            <div key={i} className="flex items-center" style={{ height: lane.height }}>
              {lane.label}
            </div>
          ))}
        </div>

        <div
          ref={areaRef}
          className={cn(
            "relative min-w-0 flex-1 select-none overflow-hidden rounded bg-bg-overlay",
            props.disabled && "pointer-events-none opacity-70",
          )}
        >
          {view ? (
            <>
              <Ruler view={view} durationS={durationS} onSeek={props.onSeek} />
              <FrameStripLane view={view} frames={props.frames} onSeek={props.onSeek} />
              <MarkersLane view={view} markers={props.markers} onSeek={props.onSeek} />
              <TakesLane
                view={view}
                takes={props.takes}
                selectedId={props.selectedTakeId}
                buffers={props.buffers}
                waveforms={props.waveforms}
                onSelect={props.onSelectTake}
                onPlay={props.onPlayTake}
                onEdit={props.onTakeEdit}
                onSeek={props.onSeek}
              />
              <GameAudioLane
                view={view}
                durationS={durationS}
                peaks={props.peaks}
                regions={props.regions}
                takes={props.takes}
                mix={props.mix}
                onRegionsChange={props.onRegionsChange}
                onSeek={props.onSeek}
              />
              {gameMarkers.map((m) => {
                const x = sToPx(view, m.at_s);
                if (x < 0 || x > view.widthPx) return null;
                return (
                  <div
                    key={`${m.at_s}-${m.label}`}
                    className="pointer-events-none absolute bottom-0 w-px bg-accent opacity-40"
                    style={{ left: x, top: LANE_HEIGHT.ruler }}
                  />
                );
              })}
              <Playhead view={view} atS={playheadS} />
            </>
          ) : (
            <p
              className="flex items-center px-3 font-mono text-xs text-text-dim"
              style={{ height: LANES.reduce((h, l) => h + l.height, 0) }}
            >
              loading video…
            </p>
          )}
        </div>
      </div>
    </section>
  );
}
