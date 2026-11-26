import { useEffect, useRef } from "react";
import { cn } from "@/lib/cn";

/** A muted, mirrored `<video>` showing a live `MediaStream` (webcam preview). */
export function LiveStreamVideo({
  stream,
  className,
}: {
  stream: MediaStream;
  className?: string;
}) {
  const ref = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    if (ref.current) ref.current.srcObject = stream;
  }, [stream]);

  return (
    <video
      ref={ref}
      autoPlay
      muted
      playsInline
      className={cn("h-full w-full -scale-x-100 object-cover", className)}
    />
  );
}
