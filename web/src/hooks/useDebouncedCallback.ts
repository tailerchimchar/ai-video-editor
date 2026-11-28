import { useCallback, useEffect, useRef } from "react";

export interface DebounceOptions {
  /** On unmount, run a pending call now instead of dropping it. */
  flushOnUnmount?: boolean;
}

/**
 * A stable function that calls `fn` once, `delayMs` after the last call.
 * A pending call is dropped on unmount, or run then with `flushOnUnmount`.
 */
export function useDebouncedCallback<A extends unknown[]>(
  fn: (...args: A) => void,
  delayMs: number,
  { flushOnUnmount = false }: DebounceOptions = {},
): (...args: A) => void {
  const fnRef = useRef(fn);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingRef = useRef<A | null>(null);
  const flushRef = useRef(flushOnUnmount);
  flushRef.current = flushOnUnmount;

  useEffect(() => {
    fnRef.current = fn;
  }, [fn]);

  useEffect(
    () => () => {
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = null;
      const pending = pendingRef.current;
      pendingRef.current = null;
      if (pending && flushRef.current) fnRef.current(...pending);
    },
    [],
  );

  return useCallback(
    (...args: A) => {
      if (timerRef.current) clearTimeout(timerRef.current);
      pendingRef.current = args;
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        pendingRef.current = null;
        fnRef.current(...args);
      }, delayMs);
    },
    [delayMs],
  );
}
