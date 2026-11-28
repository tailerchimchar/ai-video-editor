import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/cn";
import { changes, closeGaps, lensOf, shortName, type TrackClip } from "@/lib/hookTrack";
import type { HookPickView, HookView, Storyboard, Take } from "@/types/narration";

interface HookChangesBadgeProps {
  hook: HookView;
  clips: TrackClip[];
  /** Picks seen rendered (so a deleted one is listed). */
  rendered: HookPickView[];
  storyboard: Storyboard | undefined;
  takes: Take[];
  onChange: (clips: TrackClip[]) => void;
  onDiscard: () => void;
}

/**
 * "N hook changes · laptop applies": the count, and a popover with one line
 * per change (each with its own undo), close the empty spaces, discard all,
 * and what the laptop runs.
 */
export function HookChangesBadge({
  hook,
  clips,
  rendered,
  storyboard,
  takes,
  onChange,
  onDiscard,
}: HookChangesBadgeProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [open]);

  const list = changes(hook, rendered, (p) => shortName(p, storyboard));
  // Saved before this page opened and not visible as a line (e.g. a deletion).
  const lines = hook.dirty && !list.length ? [{ text: "changed earlier", undo: null }] : list;
  const n = lines.length;
  const dirty = hook.dirty || n > 0;
  const takesOverHook = takes.some((t) => t.start_s < hook.rendered_intro_s);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className={cn(
          "inline-flex items-center rounded border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider",
          dirty
            ? "border-warn bg-warn-glow text-warn"
            : "border-border bg-transparent text-text-muted",
        )}
      >
        {dirty ? `${n} hook change${n === 1 ? "" : "s"} · laptop applies ▾` : "no hook changes"}
      </button>
      {open && (
        <div className="surface-elevated absolute right-0 top-full z-40 mt-1 w-96 max-w-[90vw] space-y-2 rounded-lg border border-border p-3 text-left">
          <h3 className="font-mono text-[10px] uppercase tracking-wider text-text-muted">
            hook changes · the laptop applies them
          </h3>
          {n === 0 ? (
            <p className="font-mono text-xs text-text-dim">Nothing changed.</p>
          ) : (
            <ul className="space-y-1">
              {lines.map((c, i) => (
                <li key={i} className="flex items-center gap-2 font-mono text-xs text-text-primary">
                  <span className="min-w-0 flex-1">{c.text}</span>
                  {c.undo && (
                    <Button size="sm" onClick={() => onChange(c.undo(clips))}>
                      undo
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          )}
          {hook.dirty && takesOverHook && (
            <p className="font-mono text-xs text-warn">
              takes over the hook will not line up after the re-join
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              disabled={hook.empty_space_s < 0.05}
              onClick={() => onChange(closeGaps(clips, lensOf(hook)))}
            >
              close the empty spaces
            </Button>
            <Button size="sm" variant="danger" disabled={!hook.dirty} onClick={onDiscard}>
              discard all changes
            </Button>
          </div>
          <h3 className="pt-1 font-mono text-[10px] uppercase tracking-wider text-text-muted">
            laptop command
          </h3>
          <p className="select-all break-all rounded bg-bg-base px-2 py-1 font-mono text-xs text-text-primary">
            {hook.laptop_command}
          </p>
          <p className="select-all break-all rounded bg-bg-base px-2 py-1 font-mono text-[10px] text-text-muted">
            {hook.edits_file}
          </p>
        </div>
      )}
    </div>
  );
}
