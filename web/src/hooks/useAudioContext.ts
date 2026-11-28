import { useCallback, useEffect, useRef, useState } from "react";

export interface AudioContextHandle {
  /** Null until the first user gesture (browsers refuse audio before one). */
  ctx: AudioContext | null;
  /** Create (or resume) the context; call from a gesture handler. */
  ensure: () => AudioContext;
}

/**
 * One page-wide `AudioContext`, created lazily on the first pointer or key
 * press anywhere (or an explicit `ensure()`), closed on unmount. Decoding the
 * takes' waveforms and the mix preview both hang off it.
 */
export function useAudioContext(): AudioContextHandle {
  const ref = useRef<AudioContext | null>(null);
  const [ctx, setCtx] = useState<AudioContext | null>(null);

  const ensure = useCallback(() => {
    if (!ref.current) {
      ref.current = new AudioContext();
      setCtx(ref.current);
    }
    if (ref.current.state === "suspended") void ref.current.resume();
    return ref.current;
  }, []);

  useEffect(() => {
    // Capture phase: lanes stop the pointerdown from bubbling to the window.
    const onGesture = () => ensure();
    const opts = { once: true, capture: true } as const;
    window.addEventListener("pointerdown", onGesture, opts);
    window.addEventListener("keydown", onGesture, opts);
    return () => {
      window.removeEventListener("pointerdown", onGesture, opts);
      window.removeEventListener("keydown", onGesture, opts);
      void ref.current?.close();
      ref.current = null;
      setCtx(null);
    };
  }, [ensure]);

  return { ctx, ensure };
}
