import { useCallback, useEffect, useRef } from "react";

/**
 * Play one take's recorded audio in sync with the video: seek the video
 * to the take's start, then start both. The take's audio stops whenever
 * the video pauses or seeks elsewhere.
 */
export function useTakePlayback(video: HTMLVideoElement | null) {
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    if (!video) return;
    const halt = () => audioRef.current?.pause();
    video.addEventListener("pause", halt);
    video.addEventListener("seeking", halt);
    return () => {
      video.removeEventListener("pause", halt);
      video.removeEventListener("seeking", halt);
      halt();
    };
  }, [video]);

  return useCallback(
    (startS: number, mediaUrl: string) => {
      if (!video) return;
      const audio = (audioRef.current ??= new Audio());
      audio.pause();
      video.addEventListener(
        "seeked",
        () => {
          audio.src = mediaUrl;
          audio.currentTime = 0;
          void audio.play();
          void video.play();
        },
        { once: true },
      );
      video.currentTime = startS;
    },
    [video],
  );
}
