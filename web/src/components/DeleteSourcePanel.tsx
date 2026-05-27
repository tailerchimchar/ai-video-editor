import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { deleteAssetSource, getAsset } from "@/api/assets";
import { Button } from "./ui/Button";

interface DeleteSourcePanelProps {
  assetId: string | null | undefined;
}

/**
 * "Danger zone" panel below the history rail on the compilation viewer.
 *
 * Only renders a button when the underlying source asset is eligible
 * (source_origin === 'downloaded' AND not yet deleted). Otherwise stays
 * silent — we don't want users staring at a disabled button for a file
 * they couldn't delete anyway.
 *
 * Two-click confirm pattern (no modal) — first click arms the button,
 * second click commits. Less click ceremony than a full dialog for an
 * action that's not catastrophic (the compilation reel keeps working).
 */
export function DeleteSourcePanel({ assetId }: DeleteSourcePanelProps) {
  const qc = useQueryClient();
  const [armed, setArmed] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const asset = useQuery({
    queryKey: ["asset", assetId],
    queryFn: () => (assetId ? getAsset(assetId) : Promise.reject(new Error("no asset id"))),
    enabled: !!assetId,
  });

  const del = useMutation({
    mutationFn: () => deleteAssetSource(assetId!),
    onSuccess: (data) => {
      const mb = (data.freed_bytes / (1024 * 1024)).toFixed(1);
      setResult(
        data.already_deleted
          ? "Already deleted."
          : `Freed ${mb} MB. Compilation still plays normally; you just can't re-cut from this source.`,
      );
      setArmed(false);
      qc.invalidateQueries({ queryKey: ["asset", assetId] });
      qc.invalidateQueries({ queryKey: ["assets"] });
    },
    onError: (err: Error) => {
      setResult(`Failed: ${err.message}`);
      setArmed(false);
    },
  });

  // Show nothing while we don't know yet, or if not eligible.
  if (!asset.data) return null;
  const eligible = asset.data.source_origin === "downloaded" && !asset.data.source_deleted_at;
  if (!eligible) return null;

  const sizeHint = asset.data.filename ? ` (${asset.data.filename})` : "";

  return (
    <div className="surface-elevated space-y-3 rounded-lg border border-border bg-bg-elevated p-4">
      <div className="font-mono text-[10px] uppercase tracking-wider text-text-dim">
        danger zone · source cleanup
      </div>
      <p className="font-mono text-[11px] leading-relaxed text-text-muted">
        This compilation was made from a downloaded VOD{sizeHint}. Once you're happy with the reel,
        delete the source .mp4 to free disk space. The compilation will keep playing — you just
        can't re-cut from the source afterward.
      </p>
      <div className="flex items-center gap-2">
        {!armed ? (
          <Button
            size="sm"
            variant="ghost"
            disabled={del.isPending}
            onClick={() => {
              setArmed(true);
              setResult(null);
            }}
          >
            delete source VOD
          </Button>
        ) : (
          <>
            <Button
              size="sm"
              variant="danger"
              disabled={del.isPending}
              onClick={() => del.mutate()}
            >
              {del.isPending ? "deleting…" : "click again to confirm"}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={del.isPending}
              onClick={() => setArmed(false)}
            >
              cancel
            </Button>
          </>
        )}
      </div>
      {result && (
        <div
          className={
            del.isError
              ? "border-danger/40 bg-danger/10 rounded border p-2 font-mono text-[11px] text-danger"
              : "border-success/40 bg-success/10 rounded border p-2 font-mono text-[11px] text-success"
          }
        >
          {result}
        </div>
      )}
    </div>
  );
}
