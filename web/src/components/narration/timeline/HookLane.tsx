import { memo, useEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import { cn } from "@/lib/cn";
import {
  applyTrim,
  baseOf,
  dropAt,
  layout,
  lensOf,
  moveTo,
  offsetsText,
  removeClip,
  seekTarget,
  shortName,
  splitAt,
  trackTimeOf,
  type HookSpan,
  type TrackClip,
} from "@/lib/hookTrack";
import { pxToS, sToPx, type Viewport } from "@/lib/timeline";
import type { HookPickView, HookView, Storyboard } from "@/types/narration";
import { LANE_HEIGHT, laneX } from "./lane";

/** What the page hands the timeline for the hook lane. */
export interface HookTrackProps {
  hook: HookView;
  clips: TrackClip[];
  /** Picks the view no longer has (deleted), for their base windows. */
  extra: HookPickView[];
  storyboard: Storyboard | undefined;
  selectedId: string | null;
  onSelect: (pickId: string | null) => void;
  /** One saved change: the whole new track. */
  onChange: (clips: TrackClip[]) => void;
  onUndo: () => void;
  onRedo: () => void;
  /** A short message under the timeline (null clears it). */
  onNote: (message: string | null) => void;
}

interface HookLaneProps extends HookTrackProps {
  view: Viewport;
  video: HTMLVideoElement | null;
  /** While recording: no pointer, no keys. */
  disabled: boolean;
  onSeek: (s: number) => void;
}

/** Edge handles sit inside the clip, so two touching clips never share one. */
const EDGE_PX = 12;
const DRAG_THRESHOLD_PX = 5;
const PAD_Y = 4;
/** After a click-seek, the video is still "at" that track second within this. */
const SEEK_TOL_S = 0.05;

const isTyping = (t: EventTarget | null) => {
  const el = t as HTMLElement | null;
  return !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
};

/**
 * The hook as a track of clips, in track seconds (swipes not counted).
 * Drag an edge = trim / extend (Shift snaps 0.5 s); drag the body = move
 * (ghost + snap); click = select + seek. Keys: Delete / Backspace delete the
 * selected clip (its place stays empty), Escape deselects, Ctrl+Z undo,
 * Ctrl+Shift+Z / Ctrl+Y redo, Ctrl+S split at the playhead. A drag draws a
 * local draft and saves once on release.
 */
export const HookLane = memo(function HookLane(props: HookLaneProps) {
  const { view: vp, hook, clips, extra, storyboard, selectedId } = props;
  const [draft, setDraft] = useState<TrackClip[] | null>(null);
  const [ghost, setGhost] = useState<{ id: string; at: number; len: number } | null>(null);
  const laneRef = useRef<HTMLDivElement>(null);
  const shown = draft ?? clips;
  const lay = useMemo(() => layout(shown, hook, extra), [shown, hook, extra]);
  const lens = useMemo(() => lensOf(hook, extra), [hook, extra]);
  const picks = useMemo(() => new Map(hook.picks.map((p) => [p.id, p])), [hook.picks]);
  // The track second a click asked for, so Ctrl+S splits there exactly.
  const lastSeek = useRef<{ t: number; v: number } | null>(null);

  const seekTrack = (t: number) => {
    const saved = layout(clips, hook, extra);
    const v = seekTarget(t, saved, hook, storyboard, extra);
    if (v === null) return;
    lastSeek.current = { t, v };
    props.onSeek(v);
  };

  // Keys read the latest props through a ref; the listener is added once.
  const live = useRef({ props, clips, lay });
  live.current = { props, clips, lay };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const { props: p, clips: cs } = live.current;
      const ctrl = e.ctrlKey || e.metaKey;
      const key = e.key.toLowerCase();
      if (ctrl && key === "s") e.preventDefault(); // never the browser's save dialog
      if (p.disabled || isTyping(e.target)) return;
      if (ctrl && key === "z" && !e.shiftKey) {
        e.preventDefault();
        p.onUndo();
      } else if (ctrl && ((key === "z" && e.shiftKey) || key === "y")) {
        e.preventDefault();
        p.onRedo();
      } else if (ctrl && key === "s") {
        splitSelected(p, cs);
      } else if ((e.key === "Delete" || e.key === "Backspace") && p.selectedId) {
        if (!cs.some((c) => c.id === p.selectedId)) return;
        e.preventDefault();
        p.onNote(null);
        p.onChange(removeClip(cs, p.selectedId));
        p.onSelect(null);
      } else if (e.key === "Escape" && p.selectedId) {
        p.onSelect(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  function splitSelected(p: HookLaneProps, cs: TrackClip[]) {
    const clip = cs.find((c) => c.id === p.selectedId);
    if (!clip || !p.video) return;
    const saved = layout(cs, p.hook, p.extra);
    const v = p.video.currentTime;
    const seek = lastSeek.current;
    const t = seek && Math.abs(seek.v - v) < SEEK_TOL_S ? seek.t : trackTimeOf(v, saved, p.hook);
    if (t === null) {
      p.onNote("put the playhead inside the selected hook clip to split it");
      return;
    }
    try {
      p.onChange(splitAt(cs, clip, t, p.hook, p.extra));
      p.onNote(null);
    } catch (err) {
      p.onNote(err instanceof Error ? err.message : String(err));
    }
  }

  function trackS(e: { clientX: number }): number {
    const el = laneRef.current;
    return el ? pxToS(vp, laneX(e, el)) : 0;
  }

  /** Window listeners until release; `onMove` gets the pointer's dx in px. */
  function follow(
    e: PointerEvent,
    onMove: (dx: number, ev: globalThis.PointerEvent) => void,
    onUp: () => void,
  ) {
    const x0 = e.clientX;
    const move = (ev: globalThis.PointerEvent) => onMove(ev.clientX - x0, ev);
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      onUp();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
  }

  function startTrim(e: PointerEvent, span: HookSpan, edge: "start" | "end") {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const origin = clips;
    const c = origin.find((x) => x.id === span.clip.id) ?? span.clip;
    let last: TrackClip[] | null = null;
    props.onSelect(c.id);
    follow(
      e,
      (dx, ev) => {
        const ds = dx / vp.pxPerS;
        const step = ev.shiftKey ? 0.5 : 0.1;
        const snap = (x: number) => Math.round(x / step) * step;
        const start = edge === "start" ? snap(c.start_s - ds) : c.start_s;
        const end = edge === "end" ? snap(c.end_s + ds) : c.end_s;
        last = applyTrim(origin, c, edge, start, end, hook, extra);
        setDraft(last);
      },
      () => {
        setDraft(null);
        if (last) {
          props.onNote(null);
          props.onChange(last);
        }
      },
    );
  }

  function startMove(e: PointerEvent, span: HookSpan) {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const origin = clips;
    const c = origin.find((x) => x.id === span.clip.id) ?? span.clip;
    const tDown = trackS(e);
    let moved = false;
    let at = c.at_s;
    follow(
      e,
      (dx) => {
        if (!moved && Math.abs(dx) < DRAG_THRESHOLD_PX) return;
        moved = true;
        at = dropAt(origin, c, c.at_s + dx / vp.pxPerS, lens);
        setGhost({ id: c.id, at, len: lens(c) });
      },
      () => {
        setGhost(null);
        if (!moved) {
          props.onSelect(c.id);
          seekTrack(tDown);
        } else if (at !== c.at_s) {
          props.onNote(null);
          props.onChange(moveTo(origin, c, at, lens));
        }
      },
    );
  }

  const x = (s: number) => sToPx(vp, s);
  const height = LANE_HEIGHT.hook;
  const spanH = height - PAD_Y * 2;
  const inView = (vs: number, ve: number) => x(ve) >= 0 && x(vs) <= vp.widthPx;

  return (
    <div
      ref={laneRef}
      className="relative cursor-pointer overflow-hidden border-b border-border"
      style={{ height }}
      onPointerDown={(e) => {
        if (e.button !== 0) return;
        props.onSelect(null);
        seekTrack(trackS(e));
      }}
    >
      {lay.gaps.map((g) =>
        inView(g.vs, g.ve) ? (
          <div
            key={`gap-${g.vs}`}
            title="empty space: drop a clip here or extend into it; the laptop closes it"
            className="pointer-events-none absolute flex items-center justify-center rounded-sm border border-dashed border-text-dim font-mono text-[10px] text-text-dim"
            style={{
              left: x(g.vs),
              width: Math.max(4, x(g.ve) - x(g.vs) - 1),
              top: PAD_Y,
              height: spanH,
            }}
          >
            {x(g.ve) - x(g.vs) > 40 ? "empty" : ""}
          </div>
        ) : null,
      )}

      {lay.hooks.map((h) => {
        if (!inView(h.vs, h.ve)) return null;
        const p = picks.get(h.clip.id);
        const base = baseOf(hook, h.clip.chapter_id, extra);
        const recStart = base.base_rec_start_s - h.clip.start_s;
        // orange = differs from the mp4 (the server's `pending`), or moved by the draft
        const pending =
          !p ||
          p.pending ||
          p.at_s !== h.clip.at_s ||
          p.start_s !== h.clip.start_s ||
          p.end_s !== h.clip.end_s;
        const selected = h.clip.id === selectedId;
        const left = x(h.vs);
        const width = Math.max(6, h.len * vp.pxPerS - 1);
        const name = `${h.n + 1} · ${shortName(base, storyboard)}${h.part ? ` · part ${h.part}` : ""}`;
        const delta = offsetsText(h.clip.start_s, h.clip.end_s, " · ");
        return (
          <div key={h.clip.id}>
            <div
              title={`${base.label} · drag the body to move, the edges to trim or extend`}
              className={cn(
                "absolute cursor-grab touch-none overflow-hidden whitespace-nowrap rounded-sm border bg-accent-glow px-1.5 font-mono text-[10px] text-text-primary",
                pending ? "border-warn" : "border-accent",
                selected && "z-10 outline outline-1 outline-text-primary",
                ghost?.id === h.clip.id && "opacity-40",
              )}
              style={{ left, width, top: PAD_Y, height: spanH, lineHeight: "16px" }}
              onPointerDown={(e) => startMove(e, h)}
            >
              <span className="pointer-events-none">{name}</span>
              {delta && (
                <span className="pointer-events-none absolute bottom-0.5 right-1.5 text-warn">
                  {delta}
                </span>
              )}
              {base.events.map((ev, i) => {
                const at = ev.rec_s - recStart;
                if (at < 0 || at > h.len) return null;
                return (
                  <span
                    key={i}
                    title={ev.kind}
                    className={cn(
                      "pointer-events-none absolute bottom-0 w-0.5",
                      ev.kind === "death" ? "bg-danger" : "bg-success",
                    )}
                    style={{ left: at * vp.pxPerS - 1, height: 10 }}
                  />
                );
              })}
            </div>
            <div
              className="absolute z-20 cursor-ew-resize touch-none"
              style={{ left, width: Math.min(EDGE_PX, width / 3), top: PAD_Y, height: spanH }}
              onPointerDown={(e) => startTrim(e, h, "start")}
            />
            <div
              className="absolute z-20 cursor-ew-resize touch-none"
              style={{
                left: left + width - Math.min(EDGE_PX, width / 3),
                width: Math.min(EDGE_PX, width / 3),
                top: PAD_Y,
                height: spanH,
              }}
              onPointerDown={(e) => startTrim(e, h, "end")}
            />
            {selected && width > 28 && (
              <button
                type="button"
                title="delete (leaves empty space)"
                aria-label="delete hook clip"
                className="absolute z-30 flex h-4 w-4 items-center justify-center rounded-sm bg-bg-base font-mono text-[11px] leading-none text-text-muted hover:text-danger"
                style={{ left: left + width - 22, top: PAD_Y + 2 }}
                onPointerDown={(e) => e.stopPropagation()}
                onClick={(e) => {
                  e.stopPropagation();
                  props.onNote(null);
                  props.onChange(removeClip(clips, h.clip.id));
                  props.onSelect(null);
                }}
              >
                ×
              </button>
            )}
          </div>
        );
      })}

      {ghost && (
        <div
          className="pointer-events-none absolute z-30 rounded-sm border border-dashed border-text-primary"
          style={{ left: x(ghost.at), width: ghost.len * vp.pxPerS, top: PAD_Y, height: spanH }}
        />
      )}
    </div>
  );
});
