import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { newPickId, type TrackClip } from "@/lib/hookTrack";
import type { HookView, Storyboard } from "@/types/narration";

interface StoryboardPanelProps {
  storyboard: Storyboard | undefined;
  hook: HookView;
  clips: TrackClip[];
  /** Save a written pick list; resolves to the error message, or null. */
  onApply: (clips: TrackClip[]) => Promise<string | null>;
}

/** The storyboard with the hook as the editable pick list. */
function shownJson(storyboard: Storyboard, hook: HookView, clips: TrackClip[]): string {
  const picks = [...clips].sort((a, b) => a.at_s - b.at_s);
  return JSON.stringify(
    { ...storyboard, hook: { mode: hook.mode, title: hook.title, picks } },
    null,
    1,
  );
}

/**
 * Read what was written: only `hook.picks` is taken. A pick needs a
 * `chapter_id`; a missing `id` gets a new one, a missing `at_s` goes after
 * the previous pick. The server checks the chapters (its message shows).
 */
export function parseWritten(text: string): TrackClip[] | string {
  let obj: unknown;
  try {
    obj = JSON.parse(text);
  } catch (e) {
    return `not valid JSON: ${(e as Error).message}`;
  }
  const list = (obj as { hook?: { picks?: unknown } } | null)?.hook?.picks;
  if (!Array.isArray(list)) return "expected hook.picks to be a list";
  const out: TrackClip[] = [];
  let at = 0;
  for (const raw of list as Record<string, unknown>[]) {
    if (!raw || typeof raw.chapter_id !== "string") return "every pick needs a chapter_id";
    const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
    at = typeof raw.at_s === "number" ? Math.max(0, raw.at_s) : at;
    out.push({
      id: typeof raw.id === "string" && raw.id ? raw.id : newPickId(),
      chapter_id: raw.chapter_id,
      at_s: at,
      start_s: num(raw.start_s),
      end_s: num(raw.end_s),
    });
  }
  return out;
}

/**
 * The whole joined video as data (read-only), collapsed to its header by
 * default. "write" turns it into a text box; "apply" saves its
 * `hook.picks`.
 */
export function StoryboardPanel({ storyboard, hook, clips, onApply }: StoryboardPanelProps) {
  const [open, setOpen] = useState(false);
  const [writing, setWriting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);

  async function apply() {
    if (writing === null) return;
    const parsed = parseWritten(writing);
    if (typeof parsed === "string") {
      setError(parsed);
      return;
    }
    setApplying(true);
    const err = await onApply(parsed);
    setApplying(false);
    if (err) {
      setError(err);
      return;
    }
    setWriting(null);
    setError(null);
  }

  return (
    <section className="surface-elevated space-y-3 rounded-lg p-4">
      <header className="flex items-center gap-2">
        <button
          type="button"
          className="font-mono text-xs uppercase tracking-wider text-text-muted hover:text-text-primary"
          aria-expanded={open || writing !== null}
          onClick={() => setOpen((o) => !o)}
        >
          {writing !== null ? "storyboard · writing" : `storyboard ${open ? "▾" : "▸"}`}
        </button>
        <div className="ml-auto flex gap-1">
          {writing === null ? (
            <Button
              size="sm"
              disabled={!storyboard}
              onClick={() => {
                if (!storyboard) return;
                setWriting(shownJson(storyboard, hook, clips));
                setError(null);
              }}
            >
              write
            </Button>
          ) : (
            <>
              <Button size="sm" variant="primary" disabled={applying} onClick={() => void apply()}>
                apply
              </Button>
              <Button
                size="sm"
                onClick={() => {
                  setWriting(null);
                  setError(null);
                }}
              >
                cancel
              </Button>
            </>
          )}
        </div>
      </header>

      {writing !== null ? (
        <>
          <p className="font-mono text-xs text-text-dim">
            Only <code>hook.picks</code> is taken from what you write (chapter ids must exist;{" "}
            <code>at_s</code> is the clip&apos;s place on the hook track, missing = after the
            previous one). The rest is shown for reading.
          </p>
          <textarea
            value={writing}
            spellCheck={false}
            onChange={(e) => setWriting(e.target.value)}
            className="h-96 w-full resize-y rounded border border-warn bg-bg-base p-2 font-mono text-[10px] text-text-primary"
          />
          {error && <p className="font-mono text-xs text-danger">{error}</p>}
        </>
      ) : (
        open && (
          <>
            <p className="font-mono text-xs text-text-dim">
              The whole video as data: the hook clips with their place on the track, every game and
              chapter. Read-only until you press write. Click a hook clip on the timeline for its
              own panel.
            </p>
            {storyboard ? (
              <pre className="max-h-96 overflow-auto rounded bg-bg-base p-2 font-mono text-[10px] text-text-muted">
                {shownJson(storyboard, hook, clips)}
              </pre>
            ) : (
              <p className="font-mono text-xs text-text-dim">loading…</p>
            )}
          </>
        )
      )}
    </section>
  );
}
