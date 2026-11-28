import { useEffect, useRef } from "react";
import type { AudioContextHandle } from "@/hooks/useAudioContext";
import { regionLevelAt, scheduleFrom } from "@/lib/mixSchedule";
import type { NarrationSession } from "@/types/narration";

interface Graph {
  gameGain: GainNode;
  duckGain: GainNode;
  narrBus: GainNode;
  analyser: AnalyserNode;
}

// createMediaElementSource may be called once per element, ever.
const sources = new WeakMap<HTMLVideoElement, MediaElementAudioSourceNode>();
const graphs = new WeakMap<HTMLVideoElement, Graph>();

function buildGraph(ctx: AudioContext, video: HTMLVideoElement): Graph {
  const existing = graphs.get(video);
  if (existing && existing.gameGain.context === ctx) return existing;
  let source = sources.get(video);
  if (!source || source.context !== ctx) {
    source = ctx.createMediaElementSource(video);
    sources.set(video, source);
  }
  const gameGain = ctx.createGain();
  const duckGain = ctx.createGain();
  const narrBus = ctx.createGain();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 1024;
  source.disconnect();
  source.connect(gameGain).connect(duckGain).connect(ctx.destination);
  narrBus.connect(analyser);
  const graph = { gameGain, duckGain, narrBus, analyser };
  graphs.set(video, graph);
  return graph;
}

function setBus(ctx: AudioContext, g: Graph, on: boolean) {
  try {
    if (on) g.narrBus.connect(ctx.destination);
    else g.narrBus.disconnect(ctx.destination);
  } catch {
    // already in the wanted state
  }
}

interface Options {
  video: HTMLVideoElement | null;
  session: NarrationSession | undefined;
  buffers: Map<string, AudioBuffer>;
  /** false while recording: takes go silent, game audio stays. */
  enabled: boolean;
  /** The page's context (so decoded buffers and the graph agree). */
  audio: AudioContextHandle;
}

declare global {
  interface Window {
    __mixPreview?: { scheduled: number; duck: number };
  }
}

/**
 * Full-mix preview in the browser: the video's own audio through
 * game-gain + ducking, every unmuted take as a scheduled buffer source.
 * An approximation of the export, which stays the reference.
 */
export function useMixPreview({ video, session, buffers, enabled, audio }: Options) {
  const { ctx, ensure } = audio;

  // Latest inputs, read by the long-lived listeners.
  const live = useRef({ session, buffers, enabled });
  live.current = { session, buffers, enabled };
  const reschedule = useRef<() => void>(() => {});

  useEffect(() => {
    if (!video || !ctx) return;
    const g = buildGraph(ctx, video);
    const active = new Set<AudioBufferSourceNode>();
    let raf = 0;
    let scheduled = 0;

    const stopAll = () => {
      for (const s of active) {
        try {
          s.stop();
        } catch {
          // already stopped
        }
        s.disconnect();
      }
      active.clear();
      scheduled = 0;
    };

    const syncBus = () => setBus(ctx, g, live.current.enabled);

    const playing = () => !video.paused && !video.ended;

    const schedule = () => {
      stopAll();
      const { session: s, buffers: bufs, enabled: on } = live.current;
      if (!s || !on || !playing()) return;
      const rate = video.playbackRate || 1;
      for (const p of scheduleFrom(s.takes, video.currentTime)) {
        const take = s.takes.find((t) => t.id === p.takeId);
        const buf = take && bufs.get(take.media);
        if (!take || !buf) continue;
        const src = ctx.createBufferSource();
        src.buffer = buf;
        src.playbackRate.value = rate;
        const takeGain = ctx.createGain();
        takeGain.gain.value = p.gain;
        src.connect(takeGain).connect(g.narrBus);
        src.onended = () => {
          active.delete(src);
          src.disconnect();
        };
        src.start(ctx.currentTime + p.whenS / rate, p.offsetS, p.durationS);
        active.add(src);
        scheduled += 1;
      }
    };
    reschedule.current = schedule;

    const buf = new Float32Array(g.analyser.fftSize);
    const tick = () => {
      const mix = live.current.session?.mix;
      const regions = live.current.session?.game_regions ?? [];
      if (mix) {
        g.analyser.getFloatTimeDomainData(buf);
        let sum = 0;
        for (const v of buf) sum += v * v;
        const rms = Math.sqrt(sum / buf.length);
        const loud = rms > mix.duck_threshold;
        // While recording the takes are silent, so the game must not duck for them.
        const target = loud && live.current.enabled ? 1 / mix.duck_ratio : 1;
        const ms = loud ? mix.duck_attack_ms : mix.duck_release_ms;
        g.duckGain.gain.setTargetAtTime(target, ctx.currentTime, Math.max(ms / 1000 / 3, 0.001));
        g.gameGain.gain.setTargetAtTime(
          regionLevelAt(regions, mix.game_gain, video.currentTime),
          ctx.currentTime,
          0.02,
        );
        g.narrBus.gain.value = mix.narration_gain;
        window.__mixPreview = { scheduled, duck: g.duckGain.gain.value };
      }
      raf = requestAnimationFrame(tick);
    };

    const onPlay = () => {
      if (ctx.state === "suspended") void ctx.resume();
      syncBus();
      schedule();
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(tick);
    };
    const onStop = () => {
      cancelAnimationFrame(raf);
      stopAll();
      g.duckGain.gain.setTargetAtTime(1, ctx.currentTime, 0.02);
    };
    const onSeek = () => schedule();

    video.addEventListener("play", onPlay);
    video.addEventListener("pause", onStop);
    video.addEventListener("ended", onStop);
    video.addEventListener("seeked", onSeek);
    video.addEventListener("ratechange", onSeek);
    syncBus();
    if (playing()) onPlay();

    return () => {
      video.removeEventListener("play", onPlay);
      video.removeEventListener("pause", onStop);
      video.removeEventListener("ended", onStop);
      video.removeEventListener("seeked", onSeek);
      video.removeEventListener("ratechange", onSeek);
      cancelAnimationFrame(raf);
      stopAll();
      setBus(ctx, g, false);
      reschedule.current = () => {};
    };
  }, [video, ctx]);

  // First play is the user gesture that creates the context.
  useEffect(() => {
    if (!video) return;
    const onPlay = () => void ensure();
    video.addEventListener("play", onPlay);
    return () => video.removeEventListener("play", onPlay);
  }, [video, ensure]);

  // Edits, new buffers or the recording toggle while playing.
  const takes = session?.takes;
  useEffect(() => {
    if (!video || !ctx) return;
    const g = graphs.get(video);
    if (g) setBus(ctx, g, enabled);
    reschedule.current();
  }, [video, ctx, takes, buffers, enabled]);

  return { ctx, ensure };
}
