import { Button } from "./ui/Button";
import type { VLMFix, VLMHealth, VLMReviewResponse } from "@/api/compilations";

interface VLMReviewPanelProps {
  health: VLMHealth | undefined;
  healthLoading: boolean;
  review: VLMReviewResponse | undefined;
  reviewPending: boolean;
  reviewError: Error | null;
  onReview: () => void;
}

/**
 * Panel that surfaces the VLM taste-layer review flow.
 *
 * Renders three states:
 * 1. Backend unavailable → grey Re-review button + tooltip with the
 *    reason from `/vlm/health`.
 * 2. Backend up, no review yet → active Re-review button.
 * 3. Review returned → the button re-enables (users can re-run) and
 *    the suggested fixes list appears below. Fixes are advisory —
 *    applying them is a separate step via the editing tools.
 */
export function VLMReviewPanel({
  health,
  healthLoading,
  review,
  reviewPending,
  reviewError,
  onReview,
}: VLMReviewPanelProps) {
  const enabled = !!health?.ok && !!health.enabled;
  const label = reviewPending ? "reviewing…" : review ? "re-review with VLM" : "review with VLM";

  const disabledReason = healthLoading
    ? "checking VLM backend…"
    : !health?.enabled
      ? "VLM disabled (VLM_ENABLED=false)"
      : !health.ok
        ? (health.reason ?? "VLM backend unavailable")
        : null;

  return (
    <div className="surface-elevated space-y-3 rounded-lg p-4">
      <div className="flex items-baseline justify-between gap-2">
        <div className="font-mono text-[10px] uppercase tracking-wider text-text-dim">
          VLM taste layer
        </div>
        <div className="font-mono text-[10px] text-text-muted">
          {health?.model ? `model · ${health.model}` : health?.backend}
          {health?.latency_ms ? ` · ${health.latency_ms}ms` : null}
        </div>
      </div>

      <div>
        <Button
          size="sm"
          variant="ghost"
          disabled={!enabled || reviewPending}
          onClick={onReview}
          title={disabledReason ?? undefined}
        >
          {label}
        </Button>
      </div>

      {disabledReason && (
        <div className="rounded border border-border bg-bg-base p-2 font-mono text-[10px] text-text-muted">
          {disabledReason}
        </div>
      )}

      {reviewError && (
        <div className="border-danger/40 bg-danger/10 rounded border p-2 font-mono text-[10px] text-danger">
          review failed · {reviewError.message}
        </div>
      )}

      {review && (
        <div className="space-y-2">
          <div className="font-mono text-[10px] uppercase tracking-wider text-text-muted">
            {review.is_cohesive ? "verdict · cohesive" : `${review.fixes.length} fix(es) suggested`}
            {" · "}
            {review.passes} pass(es)
          </div>
          {review.fixes.length > 0 && (
            <ul className="space-y-1">
              {review.fixes.map((f, i) => (
                <VLMFixRow key={i} fix={f} />
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function VLMFixRow({ fix }: { fix: VLMFix }) {
  const paramBits: string[] = [];
  if (fix.fix_seconds != null) paramBits.push(`${fix.fix_seconds.toFixed(1)}s`);
  if (fix.roi) paramBits.push(fix.roi);
  if (fix.focus_x != null && fix.focus_y != null) {
    paramBits.push(`(${fix.focus_x.toFixed(2)}, ${fix.focus_y.toFixed(2)})`);
  }
  const params = paramBits.join(" · ");
  return (
    <li className="rounded border border-border bg-bg-base p-2 font-mono text-[10px] text-text-primary">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-accent">clip {fix.clip_ref}</span>
        <span className="text-text-muted">
          {fix.fix}
          {params ? ` · ${params}` : ""}
        </span>
      </div>
      <div className="mt-1 text-text-dim">{fix.issue}</div>
    </li>
  );
}
