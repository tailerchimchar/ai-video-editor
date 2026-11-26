import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  deleteTake,
  getSession,
  putCamera,
  relock,
  startExport,
  uploadTake,
} from "@/api/narration";
import type { CameraBox, SessionView, TargetRef, UploadTakeArgs } from "@/types/narration";

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
  const refetch = () => qc.invalidateQueries({ queryKey });

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
    mutationFn: () => relock(target),
    onSuccess: setView,
  });

  const exportVideo = useMutation({
    mutationFn: (force: boolean = false) => startExport(target, force),
  });

  return { session, addTake, removeTake, removeAllTakes, saveCamera, relockTakes, exportVideo };
}
