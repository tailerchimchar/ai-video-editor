import { useCallback, useEffect, useRef } from "react";
import { useMediaRecorder } from "@/hooks/useMediaRecorder";
import type { UploadTakeArgs } from "@/types/narration";

/** Shorter than this is a mis-click, not a take. */
const MIN_TAKE_S = 0.3;

interface ActiveTake {
  startS: number;
  /** Last playback position seen while recording (survives a seek). */
  lastS: number;
  hasCamera: boolean;
  detach: () => void;
}

export interface TakeCaptureOptions {
  video: HTMLVideoElement | null;
  stream: MediaStream | null;
  /** Called with the finished take; typically the upload mutation. */
  onTake: (take: UploadTakeArgs) => void;
}

export interface TakeCapture {
  recording: boolean;
  canRecord: boolean;
  start: () => void;
  stop: () => void;
}

/**
 * The sync rule between the video and the recorder:
 *
 * - start: remember `startS = video.currentTime`, start the recorder and
 *   `video.play()` together (recording while already playing is fine);
 * - stop: `durationS = video.currentTime - startS`, pause, hand the blob on;
 * - the take also stops when the video pauses, ends or is seeked (a seek
 *   would break the take's alignment, so it ends at the pre-seek time).
 */
export function useTakeCapture({ video, stream, onTake }: TakeCaptureOptions): TakeCapture {
  const recorder = useMediaRecorder();
  const activeRef = useRef<ActiveTake | null>(null);
  const onTakeRef = useRef(onTake);
  useEffect(() => {
    onTakeRef.current = onTake;
  }, [onTake]);

  const { start: startRecorder, stop: stopRecorder } = recorder;

  const finish = useCallback(
    async (endS?: number) => {
      const take = activeRef.current;
      if (!take || !video) return;
      activeRef.current = null;
      take.detach(); // before pause(), so our own pause doesn't re-enter
      const end = endS ?? video.currentTime;
      video.pause();
      const blob = await stopRecorder();
      const durationS = end - take.startS;
      if (durationS < MIN_TAKE_S) return;
      onTakeRef.current({ blob, startS: take.startS, durationS, hasCamera: take.hasCamera });
    },
    [video, stopRecorder],
  );

  const start = useCallback(() => {
    if (!video || !stream || activeRef.current) return;
    const startS = video.currentTime;
    const onTime = () => {
      if (activeRef.current) activeRef.current.lastS = video.currentTime;
    };
    const onSeeking = () => void finish(activeRef.current?.lastS);
    const onStopped = () => void finish();
    video.addEventListener("timeupdate", onTime);
    video.addEventListener("seeking", onSeeking);
    video.addEventListener("pause", onStopped);
    video.addEventListener("ended", onStopped);
    activeRef.current = {
      startS,
      lastS: startS,
      hasCamera: stream.getVideoTracks().length > 0,
      detach: () => {
        video.removeEventListener("timeupdate", onTime);
        video.removeEventListener("seeking", onSeeking);
        video.removeEventListener("pause", onStopped);
        video.removeEventListener("ended", onStopped);
      },
    };
    startRecorder(stream);
    void video.play();
  }, [video, stream, finish, startRecorder]);

  const stop = useCallback(() => void finish(), [finish]);

  // Leaving the page mid-take: drop the listeners; the stream owner stops the tracks.
  useEffect(() => () => activeRef.current?.detach(), []);

  return { recording: recorder.recording, canRecord: !!video && !!stream, start, stop };
}
