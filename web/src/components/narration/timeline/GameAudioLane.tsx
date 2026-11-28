import { memo, useEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import { cn } from "@/lib/cn";
import { isVoiceAt, regionLevelAt } from "@/lib/mixSchedule";
import { MIN_REGION_S } from "@/lib/regions";
import { pxToS, sToPx, type Viewport } from "@/lib/timeline";
import { columnPeaks } from "@/lib/waveform";
import type { AudioMix, GameAudioRegion, Peaks, Take } from "@/types/narration";
import { LANE_HEIGHT, cssVar, drawMirrorBars, laneX, sizeCanvas } from "./lane";

interface GameAudioLaneProps {
  view: Viewport;
  durationS: number;
  peaks: Peaks | undefined;
  regions: GameAudioRegion[];
  /** Unmuted takes are where the game ducks under the voice. */
  takes: Take[];
  mix: AudioMix;
  onRegionsChange: (regions: GameAudioRegion[]) => void;
  onSeek: (s: number) => void;
  /** Something else on the timeline got selected: drop the region selection. */
  deselect?: boolean;
  /** A region got selected. */
  onSelectRegion?: () => void;
}

type DragKind = "create" | "head" | "tail" | "press";

interface Drag {
  kind: DragKind;
  regionId: string | null;
  x0: number;
  anchorS: number;
  /** The free span the drag may use (neighbours and the video bounds). */
  lo: number;
  hi: number;
  moved: boolean;
}

const LEVELS = [1, 0.5, 0.25, 0];
const NEW_LEVEL = 0.5;
const DRAG_THRESHOLD_PX = 3;
const DRAFT_ID = "draft";

/** 1.0 → 0.5 → 0.25 → 0 → 1.0; any other level steps to the next one below. */
const nextLevel = (level: number) => LEVELS.find((l) => l < level - 1e-6) ?? 1;

const newRegionId = () => crypto.randomUUID().replace(/-/g, "").slice(0, 12);

/** The empty span around `s` between the regions (excluding `ignoreId`). */
function freeSpan(regions: GameAudioRegion[], s: number, durationS: number, ignoreId?: string) {
  let lo = 0;
  let hi = durationS;
  for (const r of regions) {
    if (r.id === ignoreId) continue;
    if (r.end_s <= s) lo = Math.max(lo, r.end_s);
    if (r.start_s >= s) hi = Math.min(hi, r.start_s);
  }
  return { lo, hi };
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

/**
 * The video's own audio: peaks drawn as the mix will play them (level
 * regions, game gain, a dip under the voice), with level regions on top.
 * Drag the empty lane = new region; drag a region edge = resize; click a
 * region = next level; Delete removes the selected region.
 */
export const GameAudioLane = memo(function GameAudioLane(props: GameAudioLaneProps) {
  const { view, durationS, peaks, regions, takes, mix } = props;
  const height = LANE_HEIGHT.game;
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const dragRef = useRef<Drag | null>(null);
  const [draft, setDraft] = useState<GameAudioRegion[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const shown = draft ?? regions;
  // One selection on the timeline: something else selected clears ours, and
  // selecting a region tells the timeline (so one Delete removes one thing).
  const { deselect, onSelectRegion } = props;
  useEffect(() => {
    if (deselect) setSelectedId(null);
  }, [deselect]);
  const selectRegion = (id: string) => {
    setSelectedId(id);
    onSelectRegion?.();
  };

  // The API's peaks are absolute; scale so the loudest moment fills the lane.
  const normalised = useMemo(() => {
    if (!peaks) return null;
    const max = peaks.peaks.reduce((m, p) => Math.max(m, p), 0);
    return max > 0 ? peaks.peaks.map((p) => p / max) : peaks.peaks;
  }, [peaks]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !peaks || !normalised) return;
    const cols = Math.floor(view.widthPx);
    const g = sizeCanvas(canvas, cols, height);
    if (!g) return;
    const raw = columnPeaks(normalised, peaks.rate_hz, view.scrollS, view.pxPerS, cols);
    const duck = mix.duck_ratio > 1 ? 1 / Math.sqrt(mix.duck_ratio) : 1;
    const heard = new Float32Array(cols);
    g.fillStyle = cssVar("--accent-glow");
    for (let c = 0; c < cols; c++) {
      const t = view.scrollS + (c + 0.5) / view.pxPerS;
      const voice = isVoiceAt(takes, t);
      if (voice) g.fillRect(c, 0, 1, height);
      heard[c] = (raw[c] ?? 0) * regionLevelAt(shown, mix.game_gain, t) * (voice ? duck : 1);
    }
    drawMirrorBars(g, raw, height, cssVar("--border-strong"));
    drawMirrorBars(g, heard, height, cssVar("--text-muted"));
  }, [peaks, normalised, view, takes, shown, mix.game_gain, mix.duck_ratio, height]);

  // Delete / Backspace removes the selected region (not while typing).
  const regionsRef = useRef(regions);
  regionsRef.current = regions;
  const onChangeRef = useRef(props.onRegionsChange);
  onChangeRef.current = props.onRegionsChange;
  useEffect(() => {
    if (!selectedId) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Delete" && e.key !== "Backspace") return;
      const el = e.target as HTMLElement | null;
      if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
      e.preventDefault();
      onChangeRef.current(regionsRef.current.filter((r) => r.id !== selectedId));
      setSelectedId(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId]);

  const commit = (next: GameAudioRegion[]) => {
    setDraft(null);
    props.onRegionsChange([...next].sort((a, b) => a.start_s - b.start_s));
  };

  function onPointerDown(e: PointerEvent<HTMLDivElement>) {
    if (e.button !== 0) return;
    const target = e.target as HTMLElement;
    const regionId = target.closest<HTMLElement>("[data-region-id]")?.dataset.regionId ?? null;
    const edge = target.dataset.edge as "head" | "tail" | undefined;
    const anchorS = clamp(pxToS(view, laneX(e, e.currentTarget)), 0, durationS);
    const kind: DragKind = regionId ? (edge ?? "press") : "create";
    const { lo, hi } = freeSpan(regions, anchorS, durationS, regionId ?? undefined);
    e.currentTarget.setPointerCapture(e.pointerId);
    dragRef.current = { kind, regionId, x0: e.clientX, anchorS, lo, hi, moved: false };
  }

  /** The regions as they would be if the drag ended at lane x `xPx`. */
  function draftAt(d: Drag, xPx: number): GameAudioRegion[] {
    const s = clamp(pxToS(view, xPx), d.lo, d.hi);
    if (d.kind === "create") {
      const r = {
        id: DRAFT_ID,
        start_s: Math.min(d.anchorS, s),
        end_s: Math.max(d.anchorS, s),
        level: NEW_LEVEL,
      };
      return [...regions, r];
    }
    return regions.map((r) => {
      if (r.id !== d.regionId) return r;
      if (d.kind === "head") return { ...r, start_s: Math.min(s, r.end_s - MIN_REGION_S) };
      if (d.kind === "tail") return { ...r, end_s: Math.max(s, r.start_s + MIN_REGION_S) };
      return r;
    });
  }

  function onPointerMove(e: PointerEvent<HTMLDivElement>) {
    const d = dragRef.current;
    if (!d || d.kind === "press") return;
    if (!d.moved && Math.abs(e.clientX - d.x0) < DRAG_THRESHOLD_PX) return;
    d.moved = true;
    setDraft(draftAt(d, laneX(e, e.currentTarget)));
  }

  function onPointerUp(e: PointerEvent<HTMLDivElement>) {
    const d = dragRef.current;
    dragRef.current = null;
    if (!d) return;
    if (d.moved && d.kind === "create") {
      const created = draftAt(d, laneX(e, e.currentTarget)).find((r) => r.id === DRAFT_ID);
      if (created && created.end_s - created.start_s >= MIN_REGION_S) {
        const id = newRegionId();
        selectRegion(id);
        commit([...regions, { ...created, id }]);
      } else {
        setDraft(null);
      }
      return;
    }
    if (d.moved) {
      commit(draftAt(d, laneX(e, e.currentTarget)));
      return;
    }
    if (!d.regionId) {
      setSelectedId(null);
      props.onSeek(d.anchorS);
      return;
    }
    selectRegion(d.regionId);
    commit(regions.map((r) => (r.id === d.regionId ? { ...r, level: nextLevel(r.level) } : r)));
  }

  function onPointerCancel() {
    dragRef.current = null;
    setDraft(null);
  }

  return (
    <div
      className="relative cursor-crosshair touch-none overflow-hidden"
      style={{ height }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
    >
      {peaks ? (
        <canvas ref={canvasRef} className="pointer-events-none absolute left-0 top-0" />
      ) : (
        <span className="absolute left-2 top-2 font-mono text-[10px] text-text-dim">
          reading game audio…
        </span>
      )}
      {shown.map((r) => {
        const left = sToPx(view, r.start_s);
        const width = (r.end_s - r.start_s) * view.pxPerS;
        if (left > view.widthPx || left + width < 0) return null;
        const selected = r.id === selectedId;
        return (
          <div
            key={r.id}
            data-region-id={r.id}
            title={`game audio ${Math.round(r.level * 100)}% · click for the next level, Delete removes`}
            className={cn(
              "absolute inset-y-0 cursor-pointer border-x border-warn bg-warn-glow",
              selected && "z-10 outline outline-1 outline-warn",
            )}
            style={{ left, width: Math.max(2, width) }}
          >
            <span className="pointer-events-none absolute left-1.5 top-0.5 font-mono text-[10px] text-warn">
              {Math.round(r.level * 100)}%
            </span>
            {width >= 12 && (
              <>
                <div
                  data-edge="head"
                  className="absolute inset-y-0 left-0 w-1.5 cursor-ew-resize"
                />
                <div
                  data-edge="tail"
                  className="absolute inset-y-0 right-0 w-1.5 cursor-ew-resize"
                />
              </>
            )}
          </div>
        );
      })}
    </div>
  );
});
