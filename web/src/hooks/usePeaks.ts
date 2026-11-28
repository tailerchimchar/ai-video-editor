import { useQuery } from "@tanstack/react-query";
import { getPeaks } from "@/api/narration";
import type { TargetRef } from "@/types/narration";

/** Game-audio waveform peaks. Depends on the video only, so never refetched. */
export function usePeaks(target: TargetRef) {
  return useQuery({
    queryKey: ["narration", target.kind, target.key, "peaks"],
    queryFn: () => getPeaks(target),
    staleTime: Infinity,
    retry: 1,
  });
}
