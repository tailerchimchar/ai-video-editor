import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  applySuggestions,
  getSuggestions,
  setAccepted,
  startSuggestions,
  suggestionsKey,
} from "@/api/suggestions";
import { VIDEOS_QUERY_KEY } from "@/api/videos";
import type { SuggestionSet } from "@/types/suggestions";

const POLL_MS = 1500;

/**
 * Server state for one story round's editor suggestions: the set (polled
 * while `pending`), ticking an item, starting the panel job, and apply.
 */
export function useSuggestions(round: string) {
  const qc = useQueryClient();
  const key = suggestionsKey(round);

  const set = useQuery({
    queryKey: key,
    queryFn: () => getSuggestions(round),
    refetchInterval: (query) => (query.state.data?.status === "pending" ? POLL_MS : false),
  });

  const refetch = () => {
    qc.invalidateQueries({ queryKey: key });
    qc.invalidateQueries({ queryKey: VIDEOS_QUERY_KEY });
  };

  const start = useMutation({
    mutationFn: () => startSuggestions(round),
    onSuccess: refetch,
  });

  const tick = useMutation({
    mutationFn: (args: { itemId: string; accepted: boolean | null }) =>
      setAccepted(round, args.itemId, args.accepted),
    onSuccess: (next: SuggestionSet) => qc.setQueryData(key, next),
  });

  const apply = useMutation({
    mutationFn: () => applySuggestions(round),
    onSuccess: refetch,
  });

  return { set, start, tick, apply };
}
