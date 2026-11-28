import { useEffect, useMemo, useRef, useState } from "react";
import { takeMediaUrl } from "@/api/narration";
import { mergeChannels } from "@/lib/waveform";
import type { SessionView } from "@/types/narration";

export interface TakeBuffers {
  buffers: Map<string, AudioBuffer>;
  waveforms: Map<string, Float32Array>;
}

/**
 * Decode every take's media once (keyed by `take.media`) for scheduling and
 * waveform drawing. The returned Maps are replaced only when a new buffer
 * lands. Does nothing without an AudioContext.
 */
export function useTakeBuffers(
  view: SessionView | undefined,
  ctx: AudioContext | null,
): TakeBuffers {
  const buffersRef = useRef(new Map<string, AudioBuffer>());
  const wavesRef = useRef(new Map<string, Float32Array>());
  const inFlight = useRef(new Set<string>());
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!view || !ctx) return;
    for (const take of view.session.takes) {
      const media = take.media;
      if (buffersRef.current.has(media) || inFlight.current.has(media)) continue;
      inFlight.current.add(media);
      void (async () => {
        try {
          const res = await fetch(takeMediaUrl(view, take));
          if (!res.ok) return;
          const buffer = await ctx.decodeAudioData(await res.arrayBuffer());
          const channels = Array.from({ length: buffer.numberOfChannels }, (_, i) =>
            buffer.getChannelData(i),
          );
          buffersRef.current.set(media, buffer);
          wavesRef.current.set(media, mergeChannels(channels));
          setTick((n) => n + 1);
        } catch {
          // Missing or undecodable media: the take simply has no waveform.
        } finally {
          inFlight.current.delete(media);
        }
      })();
    }
  }, [view, ctx]);

  return useMemo(
    () => ({ buffers: new Map(buffersRef.current), waveforms: new Map(wavesRef.current) }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [tick],
  );
}
