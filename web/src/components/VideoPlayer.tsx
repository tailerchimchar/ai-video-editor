import { forwardRef, useRef } from "react";
import { cn } from "@/lib/cn";

interface VideoPlayerProps {
  src: string;
  poster?: string;
  /**
   * Cache-buster — append as a `?v=` query string. Bump this on every
   * successful spec mutation so the browser actually re-fetches the
   * regenerated `compilation.mp4` instead of replaying the stale cached
   * copy. The `<video>` keyed off the resulting URL remounts cleanly.
   */
  version?: number | string;
  /** Called every time the browser fires `timeupdate` (~4 Hz). Phase 2's reel scrubber hooks here. */
  onTimeUpdate?: (currentTime: number) => void;
  className?: string;
}

/**
 * Controlled `<video>` element. Native controls for Phase 1 — Phase 2
 * adds an external scrubber bound to `onTimeUpdate`, hot-keys, etc.
 *
 * We forward the ref so callers can imperatively `pause()` / seek from
 * elsewhere if needed (Phase 2 timeline clicks).
 */
export const VideoPlayer = forwardRef<HTMLVideoElement, VideoPlayerProps>(function VideoPlayer(
  { src, poster, version, onTimeUpdate, className },
  ref,
) {
  const fallbackRef = useRef<HTMLVideoElement>(null);
  const videoRef = (ref as React.RefObject<HTMLVideoElement>) ?? fallbackRef;

  // ?v=… invalidates the browser cache when the underlying file was
  // overwritten by an ffmpeg re-render. The element is also keyed on
  // the same string so React fully remounts (loads fresh metadata).
  const versionedSrc =
    version === undefined ? src : `${src}?v=${encodeURIComponent(String(version))}`;

  return (
    <div
      className={cn(
        "surface-elevated overflow-hidden rounded-lg",
        // Aspect-ratio reservation prevents content from jumping when the
        // video loads. 16:9 is the project default; future 9:16 reels
        // would need a per-comp aspect from the API.
        "aspect-video w-full",
        className,
      )}
    >
      <video
        ref={videoRef}
        key={versionedSrc}
        src={versionedSrc}
        poster={poster}
        controls
        preload="metadata"
        className="h-full w-full bg-black"
        onTimeUpdate={(e) => onTimeUpdate?.(e.currentTarget.currentTime)}
      />
    </div>
  );
});
