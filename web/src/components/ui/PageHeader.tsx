import type { ReactNode } from "react";

interface PageHeaderProps {
  /** Serif identity line. */
  title: string;
  /** Mono or muted-sans subtitle — counts, ids, descriptors. */
  subtitle?: string;
  /** Right-aligned slot for actions (back link, buttons). */
  trailing?: ReactNode;
}

/**
 * Identity row at the top of a page. Serif title + mono subtitle is
 * the pairing rule — see plan's design language.
 */
export function PageHeader({ title, subtitle, trailing }: PageHeaderProps) {
  return (
    <header className="mb-12 flex items-end justify-between gap-8 border-b border-border pb-8">
      <div className="space-y-2">
        <h1 className="font-display text-5xl tracking-tight text-text-primary md:text-6xl">
          {title}
        </h1>
        {subtitle && <p className="font-mono text-sm text-text-muted">{subtitle}</p>}
      </div>
      {trailing && <div className="flex shrink-0 items-center gap-3">{trailing}</div>}
    </header>
  );
}
