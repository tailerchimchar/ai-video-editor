/*
 * Typed fetch wrapper.
 *
 * Every endpoint in `api/*.ts` calls `request<T>(...)`. The wrapper
 * concentrates error handling, JSON parsing, and base-URL composition
 * in one place — adding a new endpoint is a one-line consumer.
 */

import { env } from "@/lib/env";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  /** Override the request content-type — defaults to JSON. */
  contentType?: string;
}

/**
 * Issue a request and parse JSON. Throws `ApiError` on non-2xx so
 * callers (and TanStack Query) get clear error states.
 */
export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const url = `${env.apiBaseUrl}${path}`;
  const headers: Record<string, string> = {};
  let body: BodyInit | undefined;

  if (opts.body !== undefined) {
    if (opts.body instanceof FormData) {
      body = opts.body;
      // Browser sets multipart boundary; do not force content-type.
    } else {
      headers["content-type"] = opts.contentType ?? "application/json";
      body = JSON.stringify(opts.body);
    }
  }

  const res = await fetch(url, {
    method: opts.method ?? "GET",
    headers,
    body,
    signal: opts.signal,
  });

  if (!res.ok) {
    let parsedBody: unknown = null;
    try {
      parsedBody = await res.json();
    } catch {
      // Body wasn't JSON — that's fine, we surface status alone.
    }
    throw new ApiError(`${res.status} ${res.statusText} on ${path}`, res.status, parsedBody);
  }

  // 204 No Content has no body — return undefined-as-T for callers that don't care.
  if (res.status === 204) return undefined as T;

  return (await res.json()) as T;
}
