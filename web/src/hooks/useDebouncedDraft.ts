import { useCallback, useEffect, useRef, useState } from "react";
import { useDebouncedCallback } from "./useDebouncedCallback";

/**
 * A local copy of a server value for a slider: changes show at once and are
 * committed `delayMs` after the last one. The server value replaces the
 * draft only when nothing is waiting to be saved, so a save landing mid-drag
 * does not yank the slider back.
 */
export function useDebouncedDraft<T>(
  value: T,
  commit: (v: T) => void,
  delayMs: number,
): [T, (v: T) => void] {
  const [draft, setDraft] = useState(value);
  const dirty = useRef(false);
  const save = useDebouncedCallback((v: T) => {
    dirty.current = false;
    commit(v);
  }, delayMs);

  useEffect(() => {
    if (!dirty.current) setDraft(value);
  }, [value]);

  const change = useCallback(
    (v: T) => {
      dirty.current = true;
      setDraft(v);
      save(v);
    },
    [save],
  );

  return [draft, change];
}
