import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  deleteTake,
  getSession,
  patchTake,
  putCamera,
  putCameraRegions,
  putGameRegions,
  putMix,
  relock,
  splitTake,
  startExport,
  uploadTake,
} from "@/api/narration";
import { applyTakeEdit } from "@/lib/takeEdits";
import type {
  AudioMix,
  CameraBox,
  CameraRegion,
  GameAudioRegion,
  NarrationSession,
  SessionView,
  TakeEdit,
  TargetRef,
  UploadTakeArgs,
} from "@/types/narration";

/**
 * Server state for one narration target: the `SessionView` query plus
 * every mutation the page needs. Mutations that return a `SessionView`
 * write it straight into the cache; the take upload (returns a `Take`)
 * refetches.
 */
export function useNarrationSession(target: TargetRef) {
  const qc = useQueryClient();
  const queryKey = ["narration", target.kind, target.key];

  const session = useQuery({ queryKey, queryFn: () => getSession(target) });

  const setView = (view: SessionView) => qc.setQueryData(queryKey, view);
  const refetch = () => qc.invalidateQueries({ queryKey, exact: true });

  const addTake = useMutation({
    mutationFn: (args: UploadTakeArgs) => uploadTake(target, args),
    onSuccess: refetch,
  });

  const removeTake = useMutation({
    mutationFn: (takeId: string) => deleteTake(target, takeId),
    onSuccess: setView,
  });

  /** Delete every take (the "start over" answer to a stale session). */
  const removeAllTakes = useMutation({
    mutationFn: async (takeIds: string[]) => {
      for (const id of takeIds) await deleteTake(target, id);
    },
    onSettled: refetch,
  });

  const saveCamera = useMutation({
    mutationFn: (camera: CameraBox) => putCamera(target, camera),
    onSuccess: setView,
  });

  const relockTakes = useMutation({
    mutationFn: (shiftS: number) => relock(target, shiftS),
    onSuccess: setView,
  });

  /**
   * Show `patch(session)` in the cache at once; returns the previous view so
   * `rollback` can restore it if the server refuses the change.
   */
  const optimistic = async (patch: (s: NarrationSession) => NarrationSession) => {
    await qc.cancelQueries({ queryKey, exact: true });
    const previous = qc.getQueryData<SessionView>(queryKey);
    if (previous) setView({ ...previous, session: patch(previous.session) });
    return { previous };
  };
  const rollback = (_err: unknown, _vars: unknown, context?: { previous?: SessionView }) => {
    if (context?.previous) qc.setQueryData(queryKey, context.previous);
  };

  /** Optimistic: the edit shows immediately; rolled back if the server refuses it. */
  const editTake = useMutation({
    mutationFn: ({ takeId, edit }: { takeId: string; edit: TakeEdit }) =>
      patchTake(target, takeId, edit),
    onMutate: ({ takeId, edit }) =>
      optimistic((s) => ({
        ...s,
        takes: s.takes.map((t) => (t.id === takeId ? applyTakeEdit(t, edit) : t)),
      })),
    onError: rollback,
    onSuccess: setView,
  });

  const splitTakeAt = useMutation({
    mutationFn: ({ takeId, atS }: { takeId: string; atS: number }) =>
      splitTake(target, takeId, atS),
    onSuccess: setView,
  });

  const saveMix = useMutation({
    mutationFn: (mix: AudioMix) => putMix(target, mix),
    onSuccess: setView,
  });

  /** Optimistic, like `editTake`: a drawn region stays put while it saves. */
  const saveGameRegions = useMutation({
    mutationFn: (regions: GameAudioRegion[]) => putGameRegions(target, regions),
    onMutate: (regions) => optimistic((s) => ({ ...s, game_regions: regions })),
    onError: rollback,
    onSuccess: setView,
  });

  const saveCameraRegions = useMutation({
    mutationFn: (regions: CameraRegion[]) => putCameraRegions(target, regions),
    onSuccess: setView,
  });

  const exportVideo = useMutation({
    mutationFn: (force: boolean = false) => startExport(target, force),
  });

  return {
    session,
    addTake,
    removeTake,
    removeAllTakes,
    saveCamera,
    relockTakes,
    editTake,
    splitTake: splitTakeAt,
    saveMix,
    saveGameRegions,
    saveCameraRegions,
    exportVideo,
  };
}
