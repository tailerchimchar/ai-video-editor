import { useEffect, useState } from "react";

export interface VideoClock {
  currentTime: number;
  /** 0 until metadata loads. */
  duration: number;
  playing: boolean;
}

/**
 * Mirror a `<video>` element's time / duration / play state into React.
 * Pass the element itself (from a callback ref) so the listeners attach
 * whenever the element mounts or is swapped.
 */
export function useVideoClock(video: HTMLVideoElement | null): VideoClock {
  const [clock, setClock] = useState<VideoClock>({ currentTime: 0, duration: 0, playing: false });

  useEffect(() => {
    if (!video) return;
    const sync = () =>
      setClock({
        currentTime: video.currentTime,
        duration: Number.isFinite(video.duration) ? video.duration : 0,
        playing: !video.paused && !video.ended,
      });
    const events = ["timeupdate", "loadedmetadata", "durationchange", "play", "pause", "seeked"];
    for (const e of events) video.addEventListener(e, sync);
    sync();
    return () => {
      for (const e of events) video.removeEventListener(e, sync);
    };
  }, [video]);

  return clock;
}
