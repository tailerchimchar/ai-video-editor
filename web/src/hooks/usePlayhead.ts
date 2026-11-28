import { useEffect, useState } from "react";

/**
 * The video's current time in seconds. Updates every animation frame while
 * playing; on `timeupdate` / `seeked` while paused.
 */
export function usePlayhead(video: HTMLVideoElement | null): number {
  const [time, setTime] = useState(0);

  useEffect(() => {
    if (!video) return;
    let raf = 0;
    const tick = () => {
      setTime(video.currentTime);
      if (!video.paused) raf = requestAnimationFrame(tick);
    };
    const onPlay = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(tick);
    };
    const onStop = () => {
      cancelAnimationFrame(raf);
      setTime(video.currentTime);
    };
    const onSeek = () => setTime(video.currentTime);

    video.addEventListener("play", onPlay);
    video.addEventListener("pause", onStop);
    video.addEventListener("ended", onStop);
    video.addEventListener("timeupdate", onSeek);
    video.addEventListener("seeked", onSeek);
    setTime(video.currentTime);
    if (!video.paused) raf = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(raf);
      video.removeEventListener("play", onPlay);
      video.removeEventListener("pause", onStop);
      video.removeEventListener("ended", onStop);
      video.removeEventListener("timeupdate", onSeek);
      video.removeEventListener("seeked", onSeek);
    };
  }, [video]);

  return time;
}
