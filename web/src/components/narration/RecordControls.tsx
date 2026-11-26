import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/cn";

interface RecordControlsProps {
  recording: boolean;
  canRecord: boolean;
  onRecord: () => void;
  onStop: () => void;

  cameraOn: boolean;
  onCameraToggle: (on: boolean) => void;

  microphones: MediaDeviceInfo[];
  cameras: MediaDeviceInfo[];
  microphoneId: string;
  cameraId: string;
  onMicrophoneChange: (deviceId: string) => void;
  onCameraChange: (deviceId: string) => void;

  /** Mic input level, 0..1. */
  level: number;
  /** getUserMedia failure, shown instead of the meter. */
  streamError: string | null;
  uploading: boolean;
  uploadError: string | null;
}

/**
 * Record / Stop, the camera toggle, device pickers and the mic level
 * meter. Pure presentation: every action is a callback.
 */
export function RecordControls(props: RecordControlsProps) {
  const { recording, canRecord, onRecord, onStop, cameraOn, onCameraToggle } = props;

  return (
    <section className="surface-elevated space-y-4 rounded-lg p-4">
      <div className="flex flex-wrap items-center gap-3">
        {recording ? (
          <Button variant="danger" onClick={onStop}>
            ■ Stop
          </Button>
        ) : (
          <Button variant="primary" onClick={onRecord} disabled={!canRecord || props.uploading}>
            ● Record
          </Button>
        )}

        <label className="flex items-center gap-2 font-mono text-xs text-text-muted">
          <input
            type="checkbox"
            checked={cameraOn}
            disabled={recording}
            onChange={(e) => onCameraToggle(e.target.checked)}
          />
          record camera
        </label>

        {recording && <span className="font-mono text-xs text-danger">recording…</span>}
        {props.uploading && <span className="font-mono text-xs text-text-muted">saving take…</span>}
      </div>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <DevicePicker
          label="microphone"
          devices={props.microphones}
          value={props.microphoneId}
          disabled={recording}
          onChange={props.onMicrophoneChange}
        />
        {cameraOn && (
          <DevicePicker
            label="camera"
            devices={props.cameras}
            value={props.cameraId}
            disabled={recording}
            onChange={props.onCameraChange}
          />
        )}
      </div>

      {props.streamError ? (
        <p className="font-mono text-xs text-danger">
          mic/camera unavailable · {props.streamError}
        </p>
      ) : (
        <LevelMeter level={props.level} />
      )}

      {props.uploadError && (
        <p className="font-mono text-xs text-danger">take upload failed · {props.uploadError}</p>
      )}

      <p className="font-mono text-xs text-text-dim">
        Use headphones: the video's sound plays while you record, and speakers would bleed into your
        mic.
      </p>
    </section>
  );
}

function DevicePicker({
  label,
  devices,
  value,
  disabled,
  onChange,
}: {
  label: string;
  devices: MediaDeviceInfo[];
  value: string;
  disabled: boolean;
  onChange: (deviceId: string) => void;
}) {
  return (
    <label className="flex flex-col gap-1 font-mono text-xs text-text-muted">
      {label}
      <select
        className="rounded border border-border bg-bg-overlay px-2 py-1 text-text-primary"
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">default</option>
        {devices.map((d, i) => (
          <option key={d.deviceId} value={d.deviceId}>
            {d.label || `${label} ${i + 1}`}
          </option>
        ))}
      </select>
    </label>
  );
}

function LevelMeter({ level }: { level: number }) {
  return (
    <div className="flex items-center gap-3">
      <span className="font-mono text-xs text-text-muted">mic</span>
      <div className="h-2 flex-1 overflow-hidden rounded bg-bg-overlay">
        <div
          className={cn(
            "h-full transition-[width] duration-75",
            level > 0.9 ? "bg-danger" : "bg-accent",
          )}
          style={{ width: `${Math.round(level * 100)}%` }}
        />
      </div>
    </div>
  );
}
