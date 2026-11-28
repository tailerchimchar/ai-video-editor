import { useQuery } from "@tanstack/react-query";
import { getStoryboard } from "@/api/narration";
import type { TargetRef } from "@/types/narration";

/** The storyboard of a joined video; only fetched once the hook is known to exist. */
export function useStoryboard(target: TargetRef, enabled: boolean) {
  return useQuery({
    queryKey: ["narration", target.kind, target.key, "storyboard"],
    queryFn: () => getStoryboard(target),
    enabled,
    retry: false,
  });
}
