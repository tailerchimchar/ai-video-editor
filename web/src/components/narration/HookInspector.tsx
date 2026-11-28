import { useState, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/Button";
import {
  applyTrim,
  baseOf,
  r1,
  removeClip,
  shortName,
  type HookSpan,
  type TrackClip,
} from "@/lib/hookTrack";
import { clock } from "@/lib/time";
import type { HookPickView, HookView, Storyboard } from "@/types/narration";

interface HookInspectorProps {
  span: HookSpan;
  total: number;
  hook: HookView;
  clips: TrackClip[];
  extra: HookPickView[];
  storyboard: Storyboard | undefined;
  /** The joined video's key (the edits file is named after it). */
  videoKey: string;
  onChange: (clips: TrackClip[]) => void;
  /** Back to the storyboard (deselects the clip). */
  onClose: () => void;
}

/** "starts 1.0 s earlier · ends 2.1 s later", or "unchanged". */
function diffText(start_s: number, end_s: number): string {
  const parts = [
    start_s ? `starts ${Math.abs(start_s).toFixed(1)} s ${start_s > 0 ? "earlier" : "later"}` : "",
    end_s ? `ends ${Math.abs(end_s).toFixed(1)} s ${end_s > 0 ? "later" : "sooner"}` : "",
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : "unchanged";
}

/** The edits file as the PC will save it. */
function editsJson(hook: HookView, clips: TrackClip[], videoKey: string): string {
  const picks = [...clips].sort((a, b) => a.at_s - b.at_s);
  return JSON.stringify(
    {
      schema_version: 1,
      video: videoKey,
      base_digest: hook.base_digest,
      hook: { mode: hook.mode, title: hook.title, picks },
    },
    null,
    1,
  );
}

/**
 * The selected hook clip: what it shows (game clock), as rendered, the
 * offsets as numbers, reset / delete, and the edits it makes. Mount keyed
 * by the pick id and its offsets so the number fields follow drags.
 */
export function HookInspector({
  span,
  total,
  hook,
  clips,
  extra,
  storyboard,
  videoKey,
  onChange,
  onClose,
}: HookInspectorProps) {
  const clip = span.clip;
  const base = baseOf(hook, clip.chapter_id, extra);
  const pick = hook.picks.find((p) => p.id === clip.id);
  const game = storyboard?.games.find((g) => g.round === base.round);
  const [start, setStart] = useState(clip.start_s.toFixed(1));
  const [end, setEnd] = useState(clip.end_s.toFixed(1));

  const off = base.offset_s;
  const recStart = base.base_rec_start_s - clip.start_s;
  const recEnd = base.base_rec_end_s + clip.end_s;
  const pending = !pick || pick.pending;
  // An edited pick was rendered from its base window (the view has no rendered offsets).
  const shownStart = pending ? base.base_rec_start_s : recStart;
  const shownEnd = pending ? base.base_rec_end_s : recEnd;
  const beyond = Math.max(0, recEnd - base.chapter_rec_end_s);

  const trim = (edge: "start" | "end", s: number, e: number) =>
    onChange(applyTrim(clips, clip, edge, s, e, hook, extra));

  function applyFields() {
    const s = r1(Number(start) || 0);
    const e = r1(Number(end) || 0);
    if (s === clip.start_s && e === clip.end_s) return;
    trim(s !== clip.start_s ? "start" : "end", s, e);
  }
  const onEnter = (ev: KeyboardEvent<HTMLInputElement>) => {
    if (ev.key === "Enter") applyFields();
  };

  return (
    <section className="surface-elevated space-y-3 rounded-lg p-4">
      <header className="flex items-center gap-2">
        <h2 className="font-mono text-xs uppercase tracking-wider text-text-muted">
          hook clip {span.n + 1} of {total}
        </h2>
        <Button size="sm" className="ml-auto" onClick={onClose}>
          storyboard
        </Button>
      </header>

      <div>
        <p className="text-sm text-text-primary">
          {shortName(base, storyboard)}
          {span.part ? ` · part ${span.part}` : ""}
        </p>
        <p className="font-mono text-xs text-text-dim">
          {base.label}
          {game ? ` · ${game.label}` : ""} · {hook.mode} mode · {span.len.toFixed(1)} s · at{" "}
          {clock(span.vs)}
        </p>
      </div>

      <dl className="space-y-1 font-mono text-xs">
        <div className="flex justify-between gap-2">
          <dt className="text-text-muted">shows (game clock)</dt>
          <dd className="text-text-primary">
            {clock(recStart - off)} – {clock(recEnd - off)}
          </dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-text-muted">as rendered</dt>
          <dd className="text-text-dim">
            {clock(shownStart - off)} – {clock(shownEnd - off)}
          </dd>
        </div>
      </dl>

      <p
        className={
          clip.start_s || clip.end_s
            ? "font-mono text-xs text-warn"
            : "font-mono text-xs text-text-dim"
        }
      >
        {diffText(clip.start_s, clip.end_s)}
      </p>

      <div className="flex flex-wrap items-center gap-2 font-mono text-xs text-text-muted">
        <span>offset (s)</span>
        <label className="flex items-center gap-1">
          start
          <input
            type="number"
            step="0.1"
            value={start}
            onChange={(e) => setStart(e.target.value)}
            onBlur={applyFields}
            onKeyDown={onEnter}
            className="w-16 rounded border border-border bg-bg-base px-1 py-0.5 text-text-primary"
          />
        </label>
        <label className="flex items-center gap-1">
          end
          <input
            type="number"
            step="0.1"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
            onBlur={applyFields}
            onKeyDown={onEnter}
            className="w-16 rounded border border-border bg-bg-base px-1 py-0.5 text-text-primary"
          />
        </label>
      </div>

      <div className="flex gap-2">
        <Button size="sm" disabled={!clip.start_s && !clip.end_s} onClick={() => trim("end", 0, 0)}>
          reset this clip
        </Button>
        <Button
          size="sm"
          variant="danger"
          onClick={() => {
            onChange(removeClip(clips, clip.id));
            onClose();
          }}
        >
          delete (leave space)
        </Button>
      </div>

      {beyond > 0 ? (
        <p className="font-mono text-xs text-warn">
          runs {beyond.toFixed(1)} s past the chapter&apos;s footage: the laptop extends the
          chapter, then re-joins.
        </p>
      ) : (
        <p className={pending ? "font-mono text-xs text-warn" : "font-mono text-xs text-text-dim"}>
          {pending ? "changed · the laptop applies it on the next join" : "as rendered"}
        </p>
      )}

      <details className="border-t border-border pt-2">
        <summary className="cursor-pointer font-mono text-xs text-text-muted">
          details · edits.json
        </summary>
        <p className="mt-1 font-mono text-[10px] text-text-dim">
          {clip.id} · {clip.chapter_id}
        </p>
        <pre className="mt-1 max-h-64 overflow-auto rounded bg-bg-base p-2 font-mono text-[10px] text-text-muted">
          {editsJson(hook, clips, videoKey)}
        </pre>
      </details>
    </section>
  );
}
