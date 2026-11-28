import { sToPx, type Viewport } from "@/lib/timeline";

/** The vertical playhead line through every lane; hidden when out of view. */
export function Playhead({ view, atS }: { view: Viewport; atS: number }) {
  const x = sToPx(view, atS);
  if (x < 0 || x > view.widthPx) return null;
  return (
    <div className="pointer-events-none absolute inset-y-0 z-20 w-px" style={{ left: x }}>
      <div className="absolute inset-y-0 w-px bg-text-primary" />
      <div className="absolute -left-1 top-0 h-2 w-2 rotate-45 bg-text-primary" />
    </div>
  );
}
