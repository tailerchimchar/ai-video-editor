import { memo, useMemo } from "react";
import { pxToS, type Viewport } from "@/lib/timeline";
import { envelope } from "@/lib/waveform";
import type { Take, TakeEdit } from "@/types/narration";
import { LANE_HEIGHT, laneX } from "./lane";
import { TakeBlock, type TakeWave } from "./TakeBlock";

interface TakesLaneProps {
  view: Viewport;
  takes: Take[];
  selectedId: string | null;
  /** Decoded take media (`useTakeBuffers`), keyed by `take.media`. */
  buffers: Map<string, AudioBuffer>;
  waveforms: Map<string, Float32Array>;
  onSelect: (takeId: string | null) => void;
  /** A take was clicked (not dragged). */
  onPlay: (take: Take) => void;
  onEdit: (takeId: string, edit: TakeEdit) => void;
  onSeek: (s: number) => void;
}

/** 200 envelope values per second: 2 px each at the deepest zoom. */
const ENVELOPE_HZ = 200;
// Split takes share their media, so the envelope is built once per decoded channel.
const envelopes = new WeakMap<Float32Array, Float32Array>();

function waveFor(channel: Float32Array, buffer: AudioBuffer): TakeWave {
  let env = envelopes.get(channel);
  if (!env) {
    env = envelope(channel, buffer.sampleRate, ENVELOPE_HZ);
    envelopes.set(channel, env);
  }
  return { env, rateHz: ENVELOPE_HZ, mediaDurationS: buffer.duration };
}

/** The takes as blocks; click the empty lane = deselect + seek. */
export const TakesLane = memo(function TakesLane(props: TakesLaneProps) {
  const { view, takes, selectedId, buffers, waveforms } = props;

  const waves = useMemo(() => {
    const out = new Map<string, TakeWave>();
    for (const [media, channel] of waveforms) {
      const buffer = buffers.get(media);
      if (buffer) out.set(media, waveFor(channel, buffer));
    }
    return out;
  }, [buffers, waveforms]);

  return (
    <div
      className="relative cursor-pointer overflow-hidden border-b border-border"
      style={{ height: LANE_HEIGHT.takes }}
      onPointerDown={(e) => {
        props.onSelect(null);
        props.onSeek(pxToS(view, laneX(e, e.currentTarget)));
      }}
    >
      {takes.map((take) => (
        <TakeBlock
          key={take.id}
          take={take}
          view={view}
          selected={take.id === selectedId}
          wave={waves.get(take.media)}
          onClick={(t) => {
            props.onSelect(t.id);
            props.onPlay(t);
          }}
          onEdit={(t, edit) => {
            props.onSelect(t.id);
            props.onEdit(t.id, edit);
          }}
        />
      ))}
    </div>
  );
});
