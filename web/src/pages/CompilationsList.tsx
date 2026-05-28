import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { listCompilations } from "@/api/compilations";
import { CompilationCard } from "@/components/CompilationCard";
import { GalleryTabs } from "@/components/GalleryTabs";
import { ImportVodDialog } from "@/components/ImportVodDialog";
import { Button } from "@/components/ui/Button";
import { PageHeader } from "@/components/ui/PageHeader";
import { PageShell } from "@/components/ui/PageShell";

/**
 * Landing page — newest-first gallery of compilations.
 *
 * Thumbnail-first grid layout: each tile shows the per-compilation
 * poster frame (extracted at render time) plus name/date/id. Decision
 * driver — when you have 20+ reels, text rows aren't scannable; a
 * gallery lets you find "the funny one from last Tuesday" by sight.
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
  const [importOpen, setImportOpen] = useState(false);

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
        trailing={
          <div className="flex items-center gap-3">
            <GalleryTabs />
            <Button size="sm" variant="primary" onClick={() => setImportOpen(true)}>
              + import VOD
            </Button>
          </div>
        }
      />

      <ImportVodDialog open={importOpen} onClose={() => setImportOpen(false)} />

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
        <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {data.map((c, i) => (
            <CompilationCard key={c.id} compilation={c} index={i} />
          ))}
        </div>
      )}
    </PageShell>
  );
}
