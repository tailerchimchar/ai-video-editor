import { Button } from "@/components/ui/Button";
import { resetToDefault } from "@/lib/cameraBox";
import type { CameraBox } from "@/types/narration";

interface CameraSettingsProps {
  camera: CameraBox;
  onSave: (camera: CameraBox) => void;
  busy: boolean;
}

/** "Show camera in export" (= `CameraBox.enabled`), rounded corners, and reset to the game default. */
export function CameraSettings({ camera, onSave, busy }: CameraSettingsProps) {
  const isDefault = camera.x === null && camera.y === null && camera.width_frac === null;

  return (
    <div className="flex flex-wrap items-center gap-4 font-mono text-xs text-text-muted">
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={camera.enabled}
          disabled={busy}
          onChange={(e) => onSave({ ...camera, enabled: e.target.checked })}
        />
        show camera in export
      </label>
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={camera.rounded}
          disabled={busy}
          onChange={(e) => onSave({ ...camera, rounded: e.target.checked })}
        />
        rounded corners
      </label>
      <Button
        size="sm"
        variant="ghost"
        disabled={busy || isDefault}
        onClick={() => onSave(resetToDefault(camera))}
      >
        reset box
      </Button>
    </div>
  );
}
