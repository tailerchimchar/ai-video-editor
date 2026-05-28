import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { listAssets } from "@/api/assets";
import { AssetCard } from "@/components/AssetCard";
import { GalleryTabs } from "@/components/GalleryTabs";
import { Button } from "@/components/ui/Button";
import { PageHeader } from "@/components/ui/PageHeader";
import { PageShell } from "@/components/ui/PageShell";
import type { AssetSummary } from "@/api/assets";

type GameFilter = "all" | "league" | "valorant";
type OriginFilter = "all" | "imported" | "downloaded";

/**
 * Sources gallery — every raw recording the system knows about, in a
 * thumbnail grid. Same layout primitives as `CompilationsList`.
 *
 * Filters live above the grid:
 * - Game: League / Valorant / all (matched against the asset's
 *   `game` field, which gets populated by `scan_assets`).
 * - Origin: imported (scanned from Outplayed) / downloaded (ingested
 *   via URL). Useful when you want to clean up just the downloaded
 *   set.
 *
 * Filtering is client-side — the backend serves the full list
 * unfiltered (sub-second for ~1000 rows) and the UI narrows. Keeps
 * filter logic in one place + supports instant re-filter on toggle.
 */
export function AssetsList() {
  const { data, isPending, error } = useQuery({
    queryKey: ["assets", "list"],
    queryFn: () => listAssets(),
  });

  const [gameFilter, setGameFilter] = useState<GameFilter>("all");
  const [originFilter, setOriginFilter] = useState<OriginFilter>("all");

  const filtered = useMemo<AssetSummary[]>(() => {
    if (!data) return [];
    return data.filter((a) => {
      if (gameFilter !== "all") {
        const g = (a.game ?? "").toLowerCase();
        // Asset `game` is sometimes the full Outplayed string like
        // "League of Legends_05-22-..." — match on the prefix.
        if (!g.startsWith(gameFilter)) return false;
      }
      if (originFilter !== "all") {
        const o = a.source_origin ?? "imported";
        if (o !== originFilter) return false;
      }
      return true;
    });
  }, [data, gameFilter, originFilter]);

  return (
    <PageShell>
      <PageHeader
        title="Sources"
        subtitle={
          isPending
            ? "loading…"
            : data
              ? `${filtered.length} of ${data.length} ${data.length === 1 ? "recording" : "recordings"}`
              : ""
        }
        trailing={<GalleryTabs />}
      />

      {/* Filter row — small, mono labels to match the editor aesthetic. */}
      <div className="mb-6 flex flex-wrap items-center gap-3">
        <FilterGroup
          label="game"
          value={gameFilter}
          options={[
            { value: "all", label: "all" },
            { value: "league", label: "league" },
            { value: "valorant", label: "valorant" },
          ]}
          onChange={(v) => setGameFilter(v as GameFilter)}
        />
        <FilterGroup
          label="origin"
          value={originFilter}
          options={[
            { value: "all", label: "all" },
            { value: "imported", label: "imported" },
            { value: "downloaded", label: "downloaded" },
          ]}
          onChange={(v) => setOriginFilter(v as OriginFilter)}
        />
      </div>

      {error && (
        <div className="surface-elevated rounded p-6 font-mono text-sm text-danger">
          failed to load assets · {(error as Error).message}
        </div>
      )}

      {data && data.length === 0 && (
        <div className="surface-elevated rounded p-6 font-mono text-sm text-text-muted">
          no recordings indexed yet · run <span className="text-text-primary">scan_assets</span>{" "}
          from MCP or click <span className="text-text-primary">+ import VOD</span> on the
          Compilations page
        </div>
      )}

      {data && data.length > 0 && filtered.length === 0 && (
        <div className="surface-elevated rounded p-6 font-mono text-sm text-text-muted">
          no recordings match the current filters
        </div>
      )}

      {filtered.length > 0 && (
        <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {filtered.map((a, i) => (
            <AssetCard key={a.id} asset={a} index={i} />
          ))}
        </div>
      )}
    </PageShell>
  );
}

interface FilterGroupProps {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
}

function FilterGroup({ label, value, options, onChange }: FilterGroupProps) {
  return (
    <div className="flex items-center gap-2">
      <span className="font-mono text-[10px] uppercase tracking-wider text-text-dim">{label}</span>
      <div className="flex items-center gap-1 rounded border border-border bg-bg-base p-0.5">
        {options.map((opt) => (
          <Button
            key={opt.value}
            size="sm"
            variant={value === opt.value ? "primary" : "ghost"}
            onClick={() => onChange(opt.value)}
          >
            {opt.label}
          </Button>
        ))}
      </div>
    </div>
  );
}
