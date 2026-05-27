import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

/**
 * Asymmetric page shell — top bar (thin), main content (wide).
 * Generous outer padding for the "professional workspace" feel.
 */
export function PageShell({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className="relative min-h-screen bg-bg-base">
      <main
        className={cn(
          "relative z-10 mx-auto max-w-[1600px] px-8 py-16 md:px-16 md:py-20",
          className,
        )}
      >
        {children}
      </main>
    </div>
  );
}
