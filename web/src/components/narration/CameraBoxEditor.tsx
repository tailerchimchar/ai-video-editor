import { useEffect, useRef, useState, type PointerEvent } from "react";
import { LiveStreamVideo } from "@/components/narration/LiveStreamVideo";
import { useDebouncedCallback } from "@/hooks/useDebouncedCallback";
import { applyToCamera, moveBox, pixelToFrac, resizeBox, type FracBox } from "@/lib/cameraBox";
import { cn } from "@/lib/cn";
import { CAMERA_LAYOUT_FRAME, type CameraBox, type PixelBox } from "@/types/narration";

const SAVE_DEBOUNCE_MS = 400;

interface CameraBoxEditorProps {
  /** The current box from the API (`SessionView.camera_px`, 1920x1080 frame). */
  cameraPx: PixelBox;
  camera: CameraBox;
  /** Live webcam to show inside the box; null = placeholder. */
  preview: MediaStream | null;
  /** Called (debounced) with the moved/resized box. */
  onSave: (camera: CameraBox) => void;
}

interface Drag {
  mode: "move" | "resize";
  pointerX: number;
  pointerY: number;
  layerW: number;
  layerH: number;
  from: FracBox;
  /** Latest box during this drag (read on pointer-up, avoids a stale closure). */
  to: FracBox;
}

/**
 * The camera box drawn over the video. Place it inside a `relative`
 * wrapper that has the video's exact size: it fills that wrapper and
 * scales the API's pixel box to it. Drag the box to move it; drag the
 * top-left handle to resize (aspect kept, bottom-right corner fixed,
 * since the default box sits in the bottom-right).
 */
export function CameraBoxEditor({ cameraPx, camera, preview, onSave }: CameraBoxEditorProps) {
  const layerRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<Drag | null>(null);
  const [draft, setDraft] = useState<FracBox | null>(null);
  const save = useDebouncedCallback(onSave, SAVE_DEBOUNCE_MS);

  // A new box from the server replaces the local draft.
  useEffect(() => {
    if (!dragRef.current) setDraft(null);
  }, [cameraPx]);

  const box = draft ?? pixelToFrac(cameraPx, CAMERA_LAYOUT_FRAME);

  function beginDrag(mode: Drag["mode"], e: PointerEvent<HTMLElement>) {
    const layer = layerRef.current?.getBoundingClientRect();
    if (!layer) return;
    e.preventDefault();
    e.stopPropagation();
    e.currentTarget.setPointerCapture(e.pointerId);
    dragRef.current = {
      mode,
      pointerX: e.clientX,
      pointerY: e.clientY,
      layerW: layer.width,
      layerH: layer.height,
      from: box,
      to: box,
    };
  }

  function onPointerMove(e: PointerEvent<HTMLElement>) {
    const drag = dragRef.current;
    if (!drag) return;
    const dx = (e.clientX - drag.pointerX) / drag.layerW;
    const dy = (e.clientY - drag.pointerY) / drag.layerH;
    // The resize handle is top-left, so dragging left/up makes the box bigger.
    drag.to =
      drag.mode === "move" ? moveBox(drag.from, dx, dy) : resizeBox(drag.from, -dx, "bottom-right");
    setDraft(drag.to);
  }

  function endDrag() {
    const drag = dragRef.current;
    if (!drag) return;
    dragRef.current = null;
    if (drag.to !== drag.from) save(applyToCamera(camera, drag.to));
  }

  return (
    <div ref={layerRef} className="pointer-events-none absolute inset-0">
      <div
        className={cn(
          "pointer-events-auto absolute cursor-move touch-none overflow-hidden border-2 bg-black/60",
          camera.rounded && "rounded-lg",
          camera.enabled ? "border-accent" : "border-dashed border-text-muted opacity-70",
        )}
        style={{
          left: `${box.x * 100}%`,
          top: `${box.y * 100}%`,
          width: `${box.w * 100}%`,
          height: `${box.h * 100}%`,
        }}
        onPointerDown={(e) => beginDrag("move", e)}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        title={camera.enabled ? "camera box (shown in export)" : "camera box (hidden in export)"}
      >
        {preview ? (
          <LiveStreamVideo stream={preview} className="pointer-events-none" />
        ) : (
          <span className="flex h-full items-center justify-center font-mono text-[10px] uppercase tracking-wider text-text-muted">
            camera
          </span>
        )}
        <span
          aria-label="resize camera box"
          className="absolute left-0 top-0 h-3 w-3 cursor-nwse-resize bg-accent"
          onPointerDown={(e) => beginDrag("resize", e)}
          onPointerMove={onPointerMove}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
        />
      </div>
    </div>
  );
}
