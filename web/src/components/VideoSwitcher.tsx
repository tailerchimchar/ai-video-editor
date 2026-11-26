import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { listVideos, VIDEOS_QUERY_KEY } from "@/api/videos";

interface VideoSwitcherProps {
  kind: string;
  videoKey: string;
}

/**
 * Dropdown of every narratable video; picking one opens its narration page.
 * Shares the `["videos"]` query with the `/videos` page.
 */
export function VideoSwitcher({ kind, videoKey }: VideoSwitcherProps) {
  const navigate = useNavigate();
  const { data } = useQuery({ queryKey: VIDEOS_QUERY_KEY, queryFn: listVideos });
  const videos = data ?? [];
  const current = `${kind}/${videoKey}`;
  // The open video may be missing from the list (still loading): keep it selectable.
  const known = videos.some((v) => `${v.kind}/${v.key}` === current);

  return (
    <select
      aria-label="switch video"
      value={current}
      onChange={(e) => {
        const [k, ...rest] = e.target.value.split("/");
        if (k && rest.length > 0) {
          navigate(`/narrate/${encodeURIComponent(k)}/${encodeURIComponent(rest.join("/"))}`);
        }
      }}
      className="max-w-[16rem] rounded border border-border bg-bg-base px-2 py-1.5 font-mono text-[10px] uppercase tracking-wider text-text-primary"
    >
      {!known && <option value={current}>{videoKey}</option>}
      {videos.map((v) => (
        <option key={`${v.kind}/${v.key}`} value={`${v.kind}/${v.key}`}>
          {v.title}
          {v.date ? ` · ${v.date}` : ""}
        </option>
      ))}
    </select>
  );
}
