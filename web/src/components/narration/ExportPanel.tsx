import type { JobStatus } from "@/api/assets";
import { Button } from "@/components/ui/Button";

interface ExportPanelProps {
  stale: boolean;
  takeCount: number;
  onRelock: () => void;
  onDeleteAllTakes: () => void;
  /** Relock or delete-all in flight. */
  resolvingStale: boolean;

  onExport: () => void;
  /** The POST /export request is in flight. */
  starting: boolean;
  job: JobStatus | undefined;
  error: string | null;
  /** Link to `narrated.mp4`: an earlier export, or this page's once its job completes. */
  narratedUrl: string | null;
}

/**
 * Stale warning (Relock / Delete takes), the Export button, the export
 * job's status and the link to the result.
 */
export function ExportPanel(props: ExportPanelProps) {
  const { stale, takeCount, job } = props;
  const running = job?.status === "pending" || job?.status === "running";
  const canExport = !stale && takeCount > 0 && !props.starting && !running;

  return (
    <section className="surface-elevated space-y-4 rounded-lg p-4">
      <h2 className="font-mono text-xs uppercase tracking-wider text-text-muted">export</h2>

      {stale && (
        <div className="space-y-3 rounded border border-danger bg-bg-overlay p-3 font-mono text-xs text-danger">
          <p>
            The video changed after these takes were recorded, so they may no longer line up. Keep
            them as they are, or delete them and record again.
          </p>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="primary"
              disabled={props.resolvingStale}
              onClick={props.onRelock}
            >
              relock takes
            </Button>
            <Button
              size="sm"
              variant="danger"
              disabled={props.resolvingStale || takeCount === 0}
              onClick={props.onDeleteAllTakes}
            >
              delete all takes
            </Button>
          </div>
        </div>
      )}

      <div className="flex items-center gap-3">
        <Button variant="primary" disabled={!canExport} onClick={props.onExport}>
          export narrated.mp4
        </Button>
        {takeCount === 0 && (
          <span className="font-mono text-xs text-text-dim">record a take first</span>
        )}
        {(props.starting || running) && (
          <span className="font-mono text-xs text-text-muted">
            exporting · {job?.status ?? "starting"}…
          </span>
        )}
      </div>

      {job?.status === "failed" && (
        <p className="font-mono text-xs text-danger">
          export failed · {job.error ?? "unknown error"}
        </p>
      )}
      {props.error && <p className="font-mono text-xs text-danger">{props.error}</p>}

      {props.narratedUrl && (!job || job.status === "completed") && (
        <a
          href={props.narratedUrl}
          target="_blank"
          rel="noreferrer"
          className="block font-mono text-xs text-accent hover:underline"
        >
          open narrated.mp4 ↗
        </a>
      )}
    </section>
  );
}
