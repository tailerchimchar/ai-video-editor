interface SliderRowProps {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  /** The value as shown next to the slider. */
  format: (v: number) => string;
  onChange: (v: number) => void;
  disabled?: boolean;
}

/** A labelled range slider with its value in mono, for the mix and take panels. */
export function SliderRow({ label, value, format, onChange, ...range }: SliderRowProps) {
  return (
    <label className="grid grid-cols-[4rem_1fr_4.5rem] items-center gap-3 font-mono text-xs text-text-muted">
      <span>{label}</span>
      <input
        type="range"
        className="accent-accent"
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        {...range}
      />
      <span className="text-right text-text-primary">{format(value)}</span>
    </label>
  );
}
