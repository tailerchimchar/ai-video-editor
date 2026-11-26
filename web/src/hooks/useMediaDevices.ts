import { useEffect, useState } from "react";

export interface MediaDeviceLists {
  microphones: MediaDeviceInfo[];
  cameras: MediaDeviceInfo[];
}

/**
 * The machine's microphones and cameras, kept fresh on `devicechange`.
 *
 * Browsers hide device labels until a permission grant, so pass
 * `refreshKey` (e.g. the live stream) to re-list once access is granted.
 */
export function useMediaDevices(refreshKey?: unknown): MediaDeviceLists {
  const [lists, setLists] = useState<MediaDeviceLists>({ microphones: [], cameras: [] });

  useEffect(() => {
    const media = navigator.mediaDevices;
    if (!media?.enumerateDevices) return;
    let cancelled = false;

    async function refresh() {
      const all = await media.enumerateDevices();
      if (cancelled) return;
      setLists({
        microphones: all.filter((d) => d.kind === "audioinput" && d.deviceId),
        cameras: all.filter((d) => d.kind === "videoinput" && d.deviceId),
      });
    }

    void refresh();
    media.addEventListener("devicechange", refresh);
    return () => {
      cancelled = true;
      media.removeEventListener("devicechange", refresh);
    };
  }, [refreshKey]);

  return lists;
}
