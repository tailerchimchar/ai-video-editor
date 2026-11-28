import { useQuery } from "@tanstack/react-query";
import { getFrames } from "@/api/narration";
import type { TargetRef } from "@/types/narration";

/** The video's sprite-sheet frame strip. Depends on the video only, so never refetched. */
export function useFrameStrip(target: TargetRef) {
  return useQuery({
    queryKey: ["narration", target.kind, target.key, "frames"],
    queryFn: () => getFrames(target),
    staleTime: Infinity,
    retry: 1,
  });
}
