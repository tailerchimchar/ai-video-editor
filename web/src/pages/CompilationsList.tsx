import { useQuery } from "@tanstack/react-query";
import { listCompilations } from "@/api/compilations";
import { CompilationRow } from "@/components/CompilationRow";
import { PageHeader } from "@/components/ui/PageHeader";
import { PageShell } from "@/components/ui/PageShell";

/**
 * Landing page — newest-first list of compilations.
 *
 * Uses TanStack Query for caching + retry. The 30s default staleTime
 * (set in main.tsx) means re-opening the tab refetches but scrolling
 * around doesn't spam the API.
 */
export function CompilationsList() {
  const { data, isPending, error } = useQuery({
    queryKey: ["compilations", "list"],
    queryFn: () => listCompilations(50),
  });

  return (
    <PageShell>
      <PageHeader
        title="Compilations"
        subtitle={
          isPending
            ? "loading…"
            : data
              ? `${data.length} rendered ${data.length === 1 ? "reel" : "reels"}`
              : ""
        }
      />

      {error && (
        <div className="surface-elevated rounded p-6 font-mono text-sm text-danger">
          failed to load compilations · {(error as Error).message}
        </div>
      )}

      {data && data.length === 0 && (
        <div className="surface-elevated rounded p-6 font-mono text-sm text-text-muted">
          no compilations yet · run <span className="text-text-primary">compile_highlights</span>{" "}
          from MCP to make one
        </div>
      )}

      {data && data.length > 0 && (
        <div className="flex flex-col gap-3">
          {data.map((c, i) => (
            <CompilationRow key={c.id} compilation={c} index={i} />
          ))}
        </div>
      )}
    </PageShell>
  );
}
