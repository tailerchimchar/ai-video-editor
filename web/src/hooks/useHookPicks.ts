import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useRef } from "react";
import { ApiError, errorMessage } from "@/api/client";
import { getHook, putHook, resetHook } from "@/api/narration";
import { createHistory } from "@/lib/history";
import { optimisticView, toClips, type TrackClip } from "@/lib/hookTrack";
import type { HookPickView, HookView, TargetRef } from "@/types/narration";

export const STALE_HOOK_MESSAGE =
  "the laptop re-joined since these changes: discard or re-apply them";

/**
 * The hook track of a joined video: the `HookView` query plus the edits.
 * A 404 (an old map without a hook block) is not an error: `isHookless`.
 *
 * `setPicks` shows the new track at once (`optimisticView`) and saves it;
 * every saved track is a step of the undo history, and `undo` / `redo`
 * save the step they go to. `rendered` is every pick this page has seen
 * with a rendered piece, so a deleted one can still be named and put back.
 */
export function useHookPicks(target: TargetRef) {
  const qc = useQueryClient();
  const queryKey = ["narration", target.kind, target.key, "hook"];
  const storyboardKey = ["narration", target.kind, target.key, "storyboard"];

  const hook = useQuery({ queryKey, queryFn: () => getHook(target), retry: false });
  const isHookless = hook.error instanceof ApiError && hook.error.status === 404;

  // Every pick seen, by id (kept across edits so a deleted pick keeps its base window).
  const knownRef = useRef(new Map<string, HookPickView>());
  const historyRef = useRef(createHistory<TrackClip[]>());
  const seededRef = useRef(false);
  const view = hook.data;
  const known = useMemo(() => {
    for (const p of view?.picks ?? []) knownRef.current.set(p.id, p);
    if (view && !seededRef.current) {
      seededRef.current = true;
      historyRef.current.push(toClips(view));
    }
    return [...knownRef.current.values()];
  }, [view]);
  const extra = useMemo(
    () => known.filter((p) => !view?.picks.some((q) => q.id === p.id)),
    [known, view],
  );
  const rendered = useMemo(() => known.filter((p) => p.video_start_s !== null), [known]);

  const onSaved = (next: HookView) => {
    qc.setQueryData(queryKey, next);
    void qc.invalidateQueries({ queryKey: storyboardKey, exact: true });
  };

  const save = useMutation({
    mutationFn: (picks: TrackClip[]) => putHook(target, { picks }),
    onMutate: async (picks) => {
      await qc.cancelQueries({ queryKey, exact: true });
      const previous = qc.getQueryData<HookView>(queryKey);
      const next = previous ? optimisticView(previous, picks, extra) : null;
      if (next) qc.setQueryData(queryKey, next);
      return { previous };
    },
    onError: (_err, _picks, context) => {
      if (context?.previous) qc.setQueryData(queryKey, context.previous);
    },
    onSuccess: onSaved,
  });

  const discard = useMutation({
    mutationFn: () => resetHook(target),
    onSuccess: (next) => {
      onSaved(next);
      historyRef.current.push(toClips(next));
      save.reset();
    },
  });

  /** Save a new track (one undo step). Resolves to the error message, or null. */
  async function setPicks(picks: TrackClip[]): Promise<string | null> {
    try {
      const next = await save.mutateAsync(picks);
      historyRef.current.push(toClips(next));
      return null;
    } catch (err) {
      return messageOf(err);
    }
  }

  function step(picks: TrackClip[] | null) {
    if (picks) save.mutate(picks);
  }

  const failure = save.error ?? discard.error;
  const stale = (failure instanceof ApiError && failure.status === 409) || !!view?.stale_edits;
  const error = stale ? STALE_HOOK_MESSAGE : failure ? messageOf(failure) : null;

  return {
    hook,
    view,
    isHookless,
    /** Picks the view no longer has (deleted), for base windows. */
    extra,
    rendered,
    setPicks,
    undo: () => step(historyRef.current.undo()),
    redo: () => step(historyRef.current.redo()),
    reset: () => discard.mutate(),
    saving: save.isPending || discard.isPending,
    error,
    /** The error is the 409: the answer is to discard (`reset`). */
    stale,
  };
}

function messageOf(err: unknown): string {
  if (err instanceof ApiError && err.status === 409) return STALE_HOOK_MESSAGE;
  return errorMessage(err);
}
