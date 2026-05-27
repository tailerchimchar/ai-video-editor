import { useEffect, useMemo, useState } from "react";
import { Button } from "./ui/Button";
import { mmss } from "@/lib/time";
import { cn } from "@/lib/cn";
import type { CaptionSegment } from "@/types/clip";

interface CaptionEditorProps {
  /** Source-of-truth segments from the backend. */
  initialSegments: CaptionSegment[];
  /** Caption-mode hint for the header. Legacy field — for clips that
   *  haven't been touched since the unified-model migration. */
  captionMode: "segment" | "tiktok";
  /** Clip start/end source seconds — used to default the time range
   *  when adding a brand-new caption segment. */
  clipSourceStart: number;
  clipSourceEnd: number;
  /** Disabled while a save / add / remove / tiktokify is in flight. */
  busy: boolean;
  onSave: (segments: CaptionSegment[]) => void;
  onAdd: (args: { startSeconds: number; endSeconds: number; text: string }) => void;
  onRemove: (segmentIndex: number) => void;
  onTiktokify: () => void;
}

/**
 * Per-clip caption editor with add / delete / tiktokify.
 *
 * Data model: under the new unified renderer, each segment can carry
 * an optional `style.preset`. "TikTok mode" is just lots of short
 * segments tagged with `style.preset = "tiktok"`. There's no
 * client-side mode toggle — flipping styles is a server operation
 * (`tiktokify`) so the spec.json is the single source of truth.
 *
 * Operations:
 *  - Edit text inline → save button commits via `set_clip_captions`
 *  - + add button → opens a new-segment row → commits via `add_caption`
 *  - × delete on a row → commits via `remove_caption`
 *  - "TikTok-ify this clip" → commits via `tiktokify` (explode + style)
 */
export function CaptionEditor({
  initialSegments,
  captionMode,
  clipSourceStart,
  clipSourceEnd,
  busy,
  onSave,
  onAdd,
  onRemove,
  onTiktokify,
}: CaptionEditorProps) {
  const [drafts, setDrafts] = useState<string[]>(() => initialSegments.map((s) => s.text));
  const [touched, setTouched] = useState<Set<number>>(() => new Set());

  // New-segment drafting state — only visible when user clicks "+ add".
  const [adding, setAdding] = useState(false);
  const [newText, setNewText] = useState("");
  const [newStart, setNewStart] = useState<number>(clipSourceStart);
  const [newEnd, setNewEnd] = useState<number>(clipSourceStart + 2);

  // Re-sync local state when the backend returns new data (post any
  // mutation: save/add/remove/tiktokify all re-fetch and re-render).
  useEffect(() => {
    setDrafts(initialSegments.map((s) => s.text));
    setTouched(new Set());
    setAdding(false);
    setNewText("");
    // Default new-segment time slot to right after the last existing
    // segment, or to the start of the clip if there are none.
    const lastEnd = initialSegments.length
      ? Math.max(...initialSegments.map((s) => s.end_seconds))
      : clipSourceStart;
    const slotStart = Math.min(lastEnd, clipSourceEnd - 2);
    setNewStart(slotStart);
    setNewEnd(Math.min(slotStart + 2, clipSourceEnd));
  }, [initialSegments, clipSourceStart, clipSourceEnd]);

  const dirty = useMemo(
    () => drafts.some((text, i) => text !== initialSegments[i]?.text),
    [drafts, initialSegments],
  );

  function updateDraft(i: number, value: string) {
    setDrafts((prev) => {
      const next = [...prev];
      next[i] = value;
      return next;
    });
    setTouched((prev) => {
      if (prev.has(i)) return prev;
      const next = new Set(prev);
      next.add(i);
      return next;
    });
  }

  function commit() {
    if (!dirty) return;
    const payload: CaptionSegment[] = initialSegments.map((seg, i) => {
      const text = drafts[i] ?? seg.text;
      const wasTouched = touched.has(i) && text !== seg.text;
      return wasTouched
        ? {
            start_seconds: seg.start_seconds,
            end_seconds: seg.end_seconds,
            text,
            // Preserve style — if a segment carries it, the renderer needs it
            ...(seg.style ? { style: seg.style } : {}),
          }
        : seg;
    });
    onSave(payload);
  }

  function reset() {
    setDrafts(initialSegments.map((s) => s.text));
    setTouched(new Set());
  }

  function commitAdd() {
    const trimmed = newText.trim();
    if (!trimmed) return;
    if (newEnd <= newStart) return;
    onAdd({ startSeconds: newStart, endSeconds: newEnd, text: trimmed });
  }

  const hasAnyStyledSegment = initialSegments.some((s) => s.style?.preset === "tiktok");
  const looksLikeTiktok = hasAnyStyledSegment || captionMode === "tiktok";

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="font-mono text-[10px] uppercase tracking-wider text-text-dim">
          captions · {initialSegments.length} segment
          {initialSegments.length === 1 ? "" : "s"}
          {looksLikeTiktok && " · tiktok style"}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => setAdding((a) => !a)}>
            {adding ? "cancel add" : "+ add caption"}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={busy || initialSegments.length === 0}
            onClick={onTiktokify}
          >
            tiktok-ify clip
          </Button>
          <Button size="sm" variant="ghost" disabled={!dirty || busy} onClick={reset}>
            reset
          </Button>
          <Button
            size="sm"
            variant={dirty ? "primary" : "ghost"}
            disabled={!dirty || busy}
            onClick={commit}
          >
            {busy ? "saving…" : dirty ? "save edits" : "no change"}
          </Button>
        </div>
      </div>

      {/* New-segment row */}
      {adding && (
        <div className="surface-elevated border-accent/40 space-y-2 rounded border p-3">
          <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-wider text-accent">
            new caption
          </div>
          <div className="flex items-center gap-2 font-mono text-xs">
            <label className="text-text-muted">start</label>
            <input
              type="number"
              step="0.1"
              value={newStart}
              onChange={(e) => setNewStart(Number(e.target.value))}
              className="w-20 rounded border border-border bg-bg-base px-2 py-1 text-text-primary focus:border-accent focus:outline-none"
            />
            <label className="text-text-muted">end</label>
            <input
              type="number"
              step="0.1"
              value={newEnd}
              onChange={(e) => setNewEnd(Number(e.target.value))}
              className="w-20 rounded border border-border bg-bg-base px-2 py-1 text-text-primary focus:border-accent focus:outline-none"
            />
            <span className="text-text-dim">seconds</span>
          </div>
          <textarea
            value={newText}
            onChange={(e) => setNewText(e.target.value)}
            placeholder="caption text"
            rows={2}
            className="w-full resize-none rounded border border-border bg-bg-base px-2 py-1 font-mono text-sm text-text-primary placeholder:text-text-dim focus:border-accent focus:outline-none"
          />
          <div className="flex justify-end">
            <Button
              size="sm"
              variant="primary"
              disabled={!newText.trim() || newEnd <= newStart || busy}
              onClick={commitAdd}
            >
              add to clip
            </Button>
          </div>
        </div>
      )}

      {initialSegments.length === 0 ? (
        <div className="rounded border border-dashed border-border p-4 font-mono text-xs text-text-muted">
          no captions on this clip · add one above, or transcribe the asset to auto-generate
        </div>
      ) : (
        // Constrain height — a tiktok-ified clip can explode to 60-100 word
        // segments, which used to push the effects + history rail entirely
        // off-screen. Scroll inside the panel instead.
        <ol className="max-h-[420px] space-y-2 overflow-y-auto pr-1">
          {initialSegments.map((seg, i) => {
            const draft = drafts[i] ?? seg.text;
            const isDirty = draft !== seg.text;
            const isTiktokStyled = seg.style?.preset === "tiktok";
            return (
              <li
                key={`${seg.start_seconds}-${i}`}
                className={cn(
                  "bg-bg-base/40 flex items-start gap-3 rounded border p-3",
                  isDirty ? "border-accent/40" : "border-border",
                )}
              >
                <span className="mt-1 shrink-0 font-mono text-[10px] text-text-dim">
                  {mmss(seg.start_seconds)} → {mmss(seg.end_seconds)}
                </span>
                <textarea
                  value={draft}
                  disabled={busy}
                  onChange={(e) => updateDraft(i, e.target.value)}
                  rows={Math.max(1, Math.ceil(draft.length / 80))}
                  className={cn(
                    "flex-1 resize-none bg-transparent font-mono text-sm leading-relaxed text-text-primary",
                    "focus:outline-none focus:ring-0",
                    "placeholder:text-text-dim",
                  )}
                  placeholder="(empty caption)"
                />
                <div className="flex shrink-0 items-center gap-2">
                  {isTiktokStyled && (
                    <span className="font-mono text-[10px] uppercase tracking-wider text-accent">
                      tiktok
                    </span>
                  )}
                  {isDirty && (
                    <span className="font-mono text-[10px] uppercase tracking-wider text-accent">
                      edited
                    </span>
                  )}
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => onRemove(i)}
                    className="hover:border-danger/60 rounded border border-border px-2 py-0.5 font-mono text-[10px] text-text-muted transition-colors hover:text-danger disabled:cursor-not-allowed disabled:opacity-50"
                    aria-label={`remove caption ${i + 1}`}
                  >
                    ×
                  </button>
                </div>
              </li>
            );
          })}
        </ol>
      )}

      <p className="font-mono text-[10px] text-text-dim">
        edits apply to this compilation only — the master transcript stays untouched. tiktok-ify
        explodes each segment into per-word segments with the tiktok style; revert via the history
        rail to undo.
      </p>
    </div>
  );
}
