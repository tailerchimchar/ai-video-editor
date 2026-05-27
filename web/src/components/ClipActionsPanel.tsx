import { motion } from "motion/react";
import { CaptionEditor } from "./CaptionEditor";
import { Button } from "./ui/Button";
import { cn } from "@/lib/cn";
import type { CaptionSegment, Clip } from "@/types/clip";

interface ClipActionsPanelProps {
  clip: Clip;
  clipSourceStart: number;
  clipSourceEnd: number;
  /** True when any caption mutation is in flight. */
  captionsBusy: boolean;
  /** True when a zoom or focus mutation is in flight for this clip. */
  effectsBusy: boolean;
  onEditCaptions: (segments: CaptionSegment[]) => void;
  onAddCaption: (args: { startSeconds: number; endSeconds: number; text: string }) => void;
  onRemoveCaption: (segmentIndex: number) => void;
  onTiktokify: () => void;
  onAddZoom: (args: { roi: string; factor: number }) => void;
  onAddFocus: (args: { x: number; y: number; radius: number; dim: number }) => void;
}

const ZOOM_FACTOR = 1.5;

/**
 * League-baked-in zoom presets — each button is one-click. Coords live
 * in the backend's ROI table (`api/.../edits.py::_ROI_PRESETS`); we
 * just pick by name.
 */
const ZOOM_PRESETS: ReadonlyArray<{ label: string; roi: string }> = [
  { label: "scoreboard", roi: "scoreline_lol" },
  { label: "minimap", roi: "minimap_lol" },
  { label: "champion", roi: "champion_portrait_lol" },
  { label: "killfeed", roi: "killfeed_lol" },
  // Twitch-stream cam region (only meaningful for VODs that have the
  // overlay baked in). Harmless on Outplayed sources — just zooms into
  // empty game space.
  { label: "cam", roi: "streamcam_lol" },
  { label: "center", roi: "center" },
];

/**
 * Focus is a soft circular spotlight; coords are direct fractional
 * (x, y) frame positions. The "champion" focus targets the bottom-left
 * portrait region from the League HUD. Defaults for radius + dim match
 * the MCP tool's defaults so behavior is consistent across surfaces.
 */
const FOCUS_PRESETS: ReadonlyArray<{
  label: string;
  x: number;
  y: number;
  radius: number;
  dim: number;
}> = [
  { label: "on champion", x: 0.2, y: 0.93, radius: 0.18, dim: 0.4 },
  { label: "on center", x: 0.5, y: 0.5, radius: 0.25, dim: 0.4 },
];

/**
 * RIGHT-column primary editing surface: caption editor (most prominent),
 * League-flavored effect presets, and the still-stub buttons (speaker,
 * remove). This is where the user spends most of their time.
 *
 * Clip metadata + extend slider live in `ClipMetaPanel` on the left,
 * underneath the filmstrip. History rail sits below this panel in the
 * right column as a compact undo log.
 */
export function ClipActionsPanel({
  clip,
  clipSourceStart,
  clipSourceEnd,
  captionsBusy,
  effectsBusy,
  onEditCaptions,
  onAddCaption,
  onRemoveCaption,
  onTiktokify,
  onAddZoom,
  onAddFocus,
}: ClipActionsPanelProps) {
  const anyBusy = captionsBusy || effectsBusy;
  return (
    <motion.section
      key={clip.id}
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
      className={cn(
        "surface-elevated space-y-5 rounded-lg p-5",
        "transition-shadow duration-300",
        anyBusy && "shadow-[0_0_0_1px_var(--accent-glow),inset_0_1px_0_rgba(255,255,255,0.06)]",
      )}
    >
      <CaptionEditor
        initialSegments={clip.caption_segments}
        captionMode={clip.caption_mode}
        clipSourceStart={clipSourceStart}
        clipSourceEnd={clipSourceEnd}
        busy={captionsBusy}
        onSave={onEditCaptions}
        onAdd={onAddCaption}
        onRemove={onRemoveCaption}
        onTiktokify={onTiktokify}
      />

      <div className="space-y-3 border-t border-border pt-4">
        <div className="flex items-baseline justify-between">
          <div className="font-mono text-[10px] uppercase tracking-wider text-text-dim">
            effects · League presets
          </div>
          {effectsBusy && (
            <div className="font-mono text-[10px] uppercase tracking-wider text-accent">
              applying…
            </div>
          )}
        </div>

        <div className="space-y-2">
          <div className="flex items-baseline gap-2">
            <span className="w-14 font-mono text-[10px] uppercase tracking-wider text-text-muted">
              zoom
            </span>
            <span className="font-mono text-[10px] text-text-dim">{ZOOM_FACTOR}×</span>
          </div>
          <div className="flex flex-wrap gap-2 pl-16">
            {ZOOM_PRESETS.map((p) => (
              <Button
                key={p.roi}
                size="sm"
                variant="ghost"
                disabled={effectsBusy}
                onClick={() => onAddZoom({ roi: p.roi, factor: ZOOM_FACTOR })}
              >
                {p.label}
              </Button>
            ))}
          </div>
        </div>

        <div className="space-y-2">
          <div className="flex items-baseline gap-2">
            <span className="w-14 font-mono text-[10px] uppercase tracking-wider text-text-muted">
              focus
            </span>
          </div>
          <div className="flex flex-wrap gap-2 pl-16">
            {FOCUS_PRESETS.map((p) => (
              <Button
                key={p.label}
                size="sm"
                variant="ghost"
                disabled={effectsBusy}
                onClick={() => onAddFocus({ x: p.x, y: p.y, radius: p.radius, dim: p.dim })}
              >
                {p.label}
              </Button>
            ))}
          </div>
        </div>
      </div>

      <div className="border-t border-border pt-4">
        <div className="mb-3 font-mono text-[10px] uppercase tracking-wider text-text-dim">
          more actions · coming soon
        </div>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="ghost" disabled>
            mark speaker
          </Button>
          <Button size="sm" variant="danger" disabled>
            remove clip
          </Button>
        </div>
      </div>
    </motion.section>
  );
}
