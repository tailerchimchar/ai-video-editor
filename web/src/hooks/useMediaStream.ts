import { useEffect, useState } from "react";

export interface MediaStreamOptions {
  /** Hold the devices at all. Off = no stream, so the browser shows no
   * "recording" indicator and Windows doesn't duck other audio. */
  enabled: boolean;
  /** Include the camera. Off = microphone only. */
  camera: boolean;
  /** Empty / undefined = the browser's default device. */
  microphoneId?: string;
  cameraId?: string;
}

export interface MediaStreamState {
  stream: MediaStream | null;
  error: string | null;
}

/**
 * Owns one live `getUserMedia` stream: the microphone (echo
 * cancellation + noise suppression on) plus the camera when asked.
 * Re-acquires when the options change and stops every track on
 * change/unmount/disable so the mic is released and the camera light goes
 * off. Nothing is acquired while `enabled` is false.
 */
export function useMediaStream({
  enabled,
  camera,
  microphoneId,
  cameraId,
}: MediaStreamOptions): MediaStreamState {
  const [state, setState] = useState<MediaStreamState>({ stream: null, error: null });

  useEffect(() => {
    if (!enabled) {
      setState({ stream: null, error: null });
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      setState({ stream: null, error: "This browser cannot record (no getUserMedia)." });
      return;
    }
    let cancelled = false;
    let acquired: MediaStream | null = null;

    navigator.mediaDevices
      .getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          ...(microphoneId ? { deviceId: { exact: microphoneId } } : {}),
        },
        video: camera
          ? {
              width: { ideal: 1280 },
              height: { ideal: 720 },
              ...(cameraId ? { deviceId: { exact: cameraId } } : {}),
            }
          : false,
      })
      .then((stream) => {
        if (cancelled) {
          stopTracks(stream);
          return;
        }
        acquired = stream;
        setState({ stream, error: null });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setState({ stream: null, error: err instanceof Error ? err.message : String(err) });
      });

    return () => {
      cancelled = true;
      if (acquired) stopTracks(acquired);
    };
  }, [enabled, camera, microphoneId, cameraId]);

  return state;
}

function stopTracks(stream: MediaStream) {
  for (const track of stream.getTracks()) track.stop();
}
