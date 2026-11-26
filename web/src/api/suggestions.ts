/*
 * Story editor suggestions (`/api/v1/story/{round}/suggestions`, AVE-77).
 * Query key for consumers: `suggestionsKey(round)`.
 */

import type { JobStarted, SuggestionSet } from "@/types/suggestions";
import { ApiError, request } from "./client";

export const suggestionsKey = (round: string) => ["suggestions", round] as const;

const base = (round: string) => `/api/v1/story/${encodeURIComponent(round)}/suggestions`;

/** The set, or null before the first run (the API answers 404). */
export async function getSuggestions(round: string): Promise<SuggestionSet | null> {
  try {
    return await request<SuggestionSet>(base(round));
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

export function startSuggestions(round: string): Promise<JobStarted> {
  return request<JobStarted>(base(round), { method: "POST" });
}

export function setAccepted(
  round: string,
  itemId: string,
  accepted: boolean | null,
): Promise<SuggestionSet> {
  return request<SuggestionSet>(`${base(round)}/${encodeURIComponent(itemId)}`, {
    method: "PUT",
    body: { accepted },
  });
}

/** Writes the ticked items as requests, then starts a story_render job. */
export function applySuggestions(round: string): Promise<JobStarted> {
  return request<JobStarted>(`${base(round)}/apply`, { method: "POST" });
}
