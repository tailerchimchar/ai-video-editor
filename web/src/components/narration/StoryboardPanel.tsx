import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { addChapter, newPickId, type TrackClip } from "@/lib/hookTrack";
import { clock } from "@/lib/time";
import type { HookPickView, HookView, Storyboard } from "@/types/narration";

interface StoryboardPanelProps {
  storyboard: Storyboard | undefined;
  hook: HookView;
  clips: TrackClip[];
  /** Picks the view no longer has (deleted), for base windows. */
  extra: HookPickView[];
  /** Save a pick list; resolves to the error message, or null. */
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
export function StoryboardPanel({ storyboard, hook, clips, extra, onApply }: StoryboardPanelProps) {
  const [open, setOpen] = useState(false);
  const [writing, setWriting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);

  /** "add to the hook": first empty space that fits, else the end of the track. */
  async function add(chapterId: string, len: number) {
    setApplying(true);
    const err = await onApply(addChapter(clips, chapterId, len, hook, extra));
    setApplying(false);
    setError(err);
  }

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
        <>
          {storyboard && <ChapterList storyboard={storyboard} busy={applying} onAdd={add} />}
          {error && <p className="font-mono text-xs text-danger">{error}</p>}
          {open && (
            <>
              <p className="font-mono text-xs text-text-dim">
                The whole video as data: the hook clips with their place on the track, every game
                and chapter. Read-only until you press write. Click a hook clip on the timeline for
                its own panel.
              </p>
              {storyboard ? (
                <pre className="max-h-96 overflow-auto rounded bg-bg-base p-2 font-mono text-[10px] text-text-muted">
                  {shownJson(storyboard, hook, clips)}
                </pre>
              ) : (
                <p className="font-mono text-xs text-text-dim">loading…</p>
              )}
            </>
          )}
        </>
      )}
    </section>
  );
}

/** Every game's chapters (game clock); a play chapter outside the hook can be added. */
function ChapterList({
  storyboard,
  busy,
  onAdd,
}: {
  storyboard: Storyboard;
  busy: boolean;
  onAdd: (chapterId: string, len: number) => void;
}) {
  return (
    <div className="space-y-2">
      {storyboard.games.map((g) => (
        <div key={g.round}>
          <p className="font-mono text-[10px] uppercase tracking-wider text-text-muted">
            {g.label}
          </p>
          <ul className="space-y-0.5">
            {g.chapters.map((c) => (
              <li key={c.id} className="flex items-center gap-2 font-mono text-xs">
                <span className="w-24 shrink-0 text-text-dim">
                  {clock(c.game_start_s)}–{clock(c.game_end_s)}
                </span>
                <span className="truncate text-text-primary">{c.title}</span>
                {c.in_hook ? (
                  <span className="ml-auto shrink-0 text-text-dim">in the hook</span>
                ) : (
                  c.has_hook_window &&
                  c.hook_len_s !== null && (
                    <Button
                      size="sm"
                      className="ml-auto shrink-0"
                      disabled={busy}
                      onClick={() => onAdd(c.id, c.hook_len_s ?? 0)}
                    >
                      add to the hook
                    </Button>
                  )
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}
