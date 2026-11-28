import { useEffect, useState } from "react";

/**
 * The video's duration in seconds (0 until metadata loads). Unlike
 * `useVideoClock` it does not re-render on `timeupdate`, so a page that only
 * needs the length stays still while the video plays.
 */
export function useVideoDuration(video: HTMLVideoElement | null): number {
  const [duration, setDuration] = useState(0);

  useEffect(() => {
    if (!video) return;
    const sync = () => setDuration(Number.isFinite(video.duration) ? video.duration : 0);
    video.addEventListener("loadedmetadata", sync);
    video.addEventListener("durationchange", sync);
    sync();
    return () => {
      video.removeEventListener("loadedmetadata", sync);
      video.removeEventListener("durationchange", sync);
    };
  }, [video]);

  return duration;
}
