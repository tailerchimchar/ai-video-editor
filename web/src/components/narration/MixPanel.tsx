import { useDebouncedDraft } from "@/hooks/useDebouncedDraft";
import type { AudioMix } from "@/types/narration";
import { SliderRow } from "./SliderRow";

interface MixPanelProps {
  mix: AudioMix;
  onSave: (mix: AudioMix) => void;
}

const SAVE_DELAY_MS = 300;
const times = (v: number) => `×${v.toFixed(2)}`;

/**
 * Voice level, game level (outside the drawn regions) and how hard the game
 * ducks under the voice. Moves show at once and save 300 ms after the last.
 */
export function MixPanel({ mix, onSave }: MixPanelProps) {
  const [draft, change] = useDebouncedDraft(mix, onSave, SAVE_DELAY_MS);

  return (
    <section className="surface-elevated space-y-3 rounded-lg p-4">
      <h2 className="font-mono text-xs uppercase tracking-wider text-text-muted">mix</h2>
      <SliderRow
        label="voice"
        value={draft.narration_gain}
        min={0.05}
        max={2}
        step={0.05}
        format={times}
        onChange={(v) => change({ ...draft, narration_gain: v })}
      />
      <SliderRow
        label="game"
        value={draft.game_gain}
        min={0}
        max={2}
        step={0.05}
        format={times}
        onChange={(v) => change({ ...draft, game_gain: v })}
      />
      <SliderRow
        label="duck"
        value={draft.duck_ratio}
        min={1}
        max={20}
        step={0.5}
        format={(v) => (v <= 1 ? "off" : `${v}:1`)}
        onChange={(v) => change({ ...draft, duck_ratio: v })}
      />
      <p className="font-mono text-xs text-text-dim">
        Game is the level outside the regions drawn on the game lane; duck lowers it while you talk.
      </p>
    </section>
  );
}
