import { useEffect, useState } from "react";

/**
 * Live input level of `stream`'s audio, 0..1 (RMS, scaled for a meter),
 * sampled once per animation frame. 0 when there is no stream.
 */
export function useAudioLevel(stream: MediaStream | null): number {
  const [level, setLevel] = useState(0);

  useEffect(() => {
    if (!stream || stream.getAudioTracks().length === 0) {
      setLevel(0);
      return;
    }
    const ctx = new AudioContext();
    // Autoplay policy may start the context suspended until a user gesture.
    const resume = () => void ctx.resume();
    if (ctx.state === "suspended") document.addEventListener("pointerdown", resume, { once: true });
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    // The analyser is not connected to the speakers, so the owner never
    // hears themself.
    const source = ctx.createMediaStreamSource(stream);
    source.connect(analyser);
    const samples = new Float32Array(analyser.fftSize);
    let frame = 0;

    const tick = () => {
      analyser.getFloatTimeDomainData(samples);
      let sum = 0;
      for (const s of samples) sum += s * s;
      const rms = Math.sqrt(sum / samples.length);
      setLevel(Math.min(1, rms * 4));
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);

    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("pointerdown", resume);
      source.disconnect();
      void ctx.close();
    };
  }, [stream]);

  return level;
}
