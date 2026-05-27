import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { motion, AnimatePresence } from "motion/react";
import { getJob, ingestUrl } from "@/api/assets";
import { Button } from "./ui/Button";

interface ImportVodDialogProps {
  open: boolean;
  onClose: () => void;
}

/**
 * Modal: paste a Twitch / YouTube VOD URL → download into the local
 * Outplayed folder. Polls the job until completion, surfaces progress
 * + error messages.
 *
 * The download itself runs server-side via yt-dlp. This dialog is a
 * thin trigger + status display.
 */
export function ImportVodDialog({ open, onClose }: ImportVodDialogProps) {
  const qc = useQueryClient();
  const [url, setUrl] = useState("");
  const [game, setGame] = useState("league");
  const [jobId, setJobId] = useState<string | null>(null);
  const [status, setStatus] = useState<"idle" | "starting" | "downloading" | "done" | "failed">(
    "idle",
  );
  const [message, setMessage] = useState<string | null>(null);
  const pollTimer = useRef<number | null>(null);

  const start = useMutation({
    mutationFn: (args: { url: string; game: string }) => ingestUrl(args),
    onSuccess: (resp) => {
      setJobId(resp.job_id);
      setStatus("downloading");
      setMessage("yt-dlp is downloading… long VODs can take several minutes.");
    },
    onError: (err: Error) => {
      setStatus("failed");
      setMessage(err.message);
    },
  });

  // Poll the job once it's running. 3s interval is plenty — yt-dlp
  // downloads are minutes-scale, not seconds.
  useEffect(() => {
    if (status !== "downloading" || !jobId) return;
    let cancelled = false;
    async function poll() {
      if (!jobId) return;
      try {
        const job = await getJob(jobId);
        if (cancelled) return;
        if (job.status === "completed") {
          setStatus("done");
          setMessage(
            `Asset id ${job.output_path?.slice(0, 8)}… registered. Run analyze + compile to make a reel.`,
          );
          // Refresh anything that lists assets / compilations in the background.
          qc.invalidateQueries({ queryKey: ["assets"] });
          qc.invalidateQueries({ queryKey: ["compilations"] });
          return;
        }
        if (job.status === "failed") {
          setStatus("failed");
          setMessage(job.error ?? "yt-dlp failed (no error message)");
          return;
        }
        pollTimer.current = window.setTimeout(poll, 3000);
      } catch (err) {
        if (cancelled) return;
        setStatus("failed");
        setMessage((err as Error).message);
      }
    }
    poll();
    return () => {
      cancelled = true;
      if (pollTimer.current) {
        window.clearTimeout(pollTimer.current);
        pollTimer.current = null;
      }
    };
  }, [status, jobId, qc]);

  // Reset state every time the dialog closes so opening it again is fresh.
  useEffect(() => {
    if (!open) {
      setUrl("");
      setJobId(null);
      setStatus("idle");
      setMessage(null);
    }
  }, [open]);

  function submit() {
    if (!url.trim()) return;
    setStatus("starting");
    setMessage(null);
    start.mutate({ url: url.trim(), game });
  }

  const busy = status === "starting" || status === "downloading";

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.18 }}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
          onClick={() => !busy && onClose()}
        >
          <motion.div
            initial={{ opacity: 0, y: 12, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 12, scale: 0.97 }}
            transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
            className="surface-elevated relative w-full max-w-lg space-y-4 rounded-lg border border-border bg-bg-elevated p-6"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="space-y-1">
              <h2 className="font-serif text-2xl text-text-primary">Import VOD</h2>
              <p className="font-mono text-[10px] uppercase tracking-wider text-text-dim">
                paste a twitch / youtube URL · downloads to your outplayed folder
              </p>
            </div>

            <div className="space-y-2">
              <label className="font-mono text-[10px] uppercase tracking-wider text-text-muted">
                VOD URL
              </label>
              <input
                type="url"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://www.twitch.tv/videos/…"
                disabled={busy}
                className="w-full rounded border border-border bg-bg-base px-3 py-2 font-mono text-sm text-text-primary placeholder:text-text-dim focus:border-accent focus:outline-none disabled:opacity-50"
              />
            </div>

            <div className="space-y-2">
              <label className="font-mono text-[10px] uppercase tracking-wider text-text-muted">
                Game folder
              </label>
              <select
                value={game}
                onChange={(e) => setGame(e.target.value)}
                disabled={busy}
                className="w-full rounded border border-border bg-bg-base px-3 py-2 font-mono text-sm text-text-primary focus:border-accent focus:outline-none disabled:opacity-50"
              >
                <option value="league">league</option>
                <option value="valorant">valorant</option>
              </select>
            </div>

            {message && (
              <div
                className={
                  status === "failed"
                    ? "border-danger/40 bg-danger/10 rounded border p-3 font-mono text-xs text-danger"
                    : status === "done"
                      ? "border-success/40 bg-success/10 rounded border p-3 font-mono text-xs text-success"
                      : "rounded border border-border bg-bg-base p-3 font-mono text-xs text-text-muted"
                }
              >
                {message}
              </div>
            )}

            <div className="flex items-center justify-end gap-2 pt-2">
              <Button variant="ghost" size="sm" disabled={busy} onClick={onClose}>
                {status === "done" ? "close" : "cancel"}
              </Button>
              {status !== "done" && (
                <Button variant="primary" size="sm" disabled={!url.trim() || busy} onClick={submit}>
                  {status === "starting"
                    ? "starting…"
                    : status === "downloading"
                      ? "downloading…"
                      : "import"}
                </Button>
              )}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
