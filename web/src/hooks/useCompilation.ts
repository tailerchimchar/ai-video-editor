import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import {
  getCompilation,
  getCompilationClips,
  getCompilationHistory,
  getVLMHealth,
  vlmReviewCompilation,
} from "@/api/compilations";
import {
  addClipCaption,
  addFocusEffect,
  addZoomEffect,
  extendClip,
  removeClipCaption,
  reorderClipsExplicit,
  revertCompilation,
  setClipCaptions,
  tiktokifyClipCaptions,
  type AddCaptionArgs,
  type AddFocusEffectArgs,
  type AddZoomEffectArgs,
  type ExtendClipArgs,
  type RemoveCaptionArgs,
  type SetClipCaptionsArgs,
} from "@/api/edits";

/**
 * Composite hook for the viewer page. Pulls three parallel queries
 * (metadata, clips, history) and exposes the mutations the page
 * needs (extend, revert).
 *
 * Invalidation strategy: after a successful mutation, invalidate ALL
 * three queries (metadata changes are rare but possible if intros
 * shift output_path; clips definitely change; history grows). The
 * background refetch is fast — three small JSON endpoints.
 */
export function useCompilation(compilationId: string) {
  const qc = useQueryClient();

  const metadata = useQuery({
    queryKey: ["compilation", compilationId, "metadata"],
    queryFn: () => getCompilation(compilationId),
  });

  const clips = useQuery({
    queryKey: ["compilation", compilationId, "clips"],
    queryFn: () => getCompilationClips(compilationId),
  });

  const history = useQuery({
    queryKey: ["compilation", compilationId, "history"],
    queryFn: () => getCompilationHistory(compilationId),
  });

  function invalidate() {
    qc.invalidateQueries({ queryKey: ["compilation", compilationId] });
  }

  const extend = useMutation({
    mutationFn: (args: ExtendClipArgs) => extendClip(compilationId, args),
    onSuccess: invalidate,
  });

  const revert = useMutation({
    mutationFn: (args: { to_version?: number; steps?: number }) =>
      revertCompilation(compilationId, args),
    onSuccess: invalidate,
  });

  const editCaptions = useMutation({
    mutationFn: (args: SetClipCaptionsArgs) => setClipCaptions(compilationId, args),
    onSuccess: invalidate,
  });

  const addCaption = useMutation({
    mutationFn: (args: AddCaptionArgs) => addClipCaption(compilationId, args),
    onSuccess: invalidate,
  });

  const removeCaption = useMutation({
    mutationFn: (args: RemoveCaptionArgs) => removeClipCaption(compilationId, args),
    onSuccess: invalidate,
  });

  const tiktokify = useMutation({
    mutationFn: (clipRef: string) => tiktokifyClipCaptions(compilationId, clipRef),
    onSuccess: invalidate,
  });

  const addZoom = useMutation({
    mutationFn: (args: AddZoomEffectArgs) => addZoomEffect(compilationId, args),
    onSuccess: invalidate,
  });

  const addFocus = useMutation({
    mutationFn: (args: AddFocusEffectArgs) => addFocusEffect(compilationId, args),
    onSuccess: invalidate,
  });

  const reorder = useMutation({
    mutationFn: (clipIds: string[]) => reorderClipsExplicit(compilationId, clipIds),
    onSuccess: invalidate,
  });

  // VLM taste-layer. Health is a separate low-frequency query so the
  // "Re-review with VLM" button can grey out when the backend is
  // unreachable. Refetch every 30s so a newly-started Ollama gets
  // picked up without a manual refresh.
  const vlmHealth = useQuery({
    queryKey: ["vlm", "health"],
    queryFn: getVLMHealth,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  const vlmReview = useMutation({
    mutationFn: () => vlmReviewCompilation(compilationId),
    onSuccess: invalidate,
  });

  return {
    metadata,
    clips,
    history,
    extend,
    revert,
    editCaptions,
    addCaption,
    removeCaption,
    tiktokify,
    addZoom,
    addFocus,
    reorder,
    vlmHealth,
    vlmReview,
    invalidate,
  };
}
