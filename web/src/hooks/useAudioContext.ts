import { useCallback, useEffect, useRef, useState } from "react";

export interface AudioContextHandle {
  ctx: AudioContext | null;
  /** Create the context on first call (call from a user gesture). */
  ensure: () => AudioContext;
}

/** One lazily created `AudioContext` per component, closed on unmount. */
export function useAudioContext(): AudioContextHandle {
  const ref = useRef<AudioContext | null>(null);
  const [ctx, setCtx] = useState<AudioContext | null>(null);

  const ensure = useCallback(() => {
    if (!ref.current) {
      ref.current = new AudioContext();
      setCtx(ref.current);
    }
    return ref.current;
  }, []);

  useEffect(
    () => () => {
      void ref.current?.close();
      ref.current = null;
    },
    [],
  );

  return { ctx, ensure };
}
