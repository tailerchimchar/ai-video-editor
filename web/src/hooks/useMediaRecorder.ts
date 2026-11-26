import { useCallback, useRef, useState } from "react";

/** Preferred containers, best first. The backend accepts WebM only. */
const VIDEO_MIME_TYPES = ["video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus", "video/webm"];
const AUDIO_MIME_TYPES = ["audio/webm;codecs=opus", "audio/webm"];

export interface MediaRecorderControls {
  recording: boolean;
  /** Start recording `stream`; video is captured when it has a video track. */
  start: (stream: MediaStream) => void;
  /** Stop and resolve with everything recorded since `start`. */
  stop: () => Promise<Blob>;
}

/**
 * Thin wrapper over `MediaRecorder`: start → stop → one WebM `Blob`.
 * Knows nothing about videos, takes or uploads.
 */
export function useMediaRecorder(): MediaRecorderControls {
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const [recording, setRecording] = useState(false);

  const start = useCallback((stream: MediaStream) => {
    if (recorderRef.current) return;
    const hasVideo = stream.getVideoTracks().length > 0;
    const mimeType = pickMimeType(hasVideo ? VIDEO_MIME_TYPES : AUDIO_MIME_TYPES);
    const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    chunksRef.current = [];
    recorder.ondataavailable = (e) => {
      if (e.data.size > 0) chunksRef.current.push(e.data);
    };
    recorder.start(1000); // 1 s chunks: a crash mid-take loses at most a second
    recorderRef.current = recorder;
    setRecording(true);
  }, []);

  const stop = useCallback(() => {
    const recorder = recorderRef.current;
    if (!recorder) return Promise.reject(new Error("Not recording."));
    recorderRef.current = null;
    return new Promise<Blob>((resolve) => {
      recorder.onstop = () => {
        setRecording(false);
        resolve(new Blob(chunksRef.current, { type: recorder.mimeType }));
        chunksRef.current = [];
      };
      recorder.stop();
    });
  }, []);

  return { recording, start, stop };
}

function pickMimeType(candidates: string[]): string | undefined {
  return candidates.find((t) => MediaRecorder.isTypeSupported(t));
}
