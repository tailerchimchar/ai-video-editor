import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

type Tone = "neutral" | "accent" | "muted" | "danger" | "success";

const TONE_CLASSES: Record<Tone, string> = {
  neutral: "border-border bg-bg-overlay text-text-primary",
  accent: "border-accent/40 bg-accent/10 text-accent",
  muted: "border-border bg-transparent text-text-muted",
  danger: "border-danger/40 bg-danger/10 text-danger",
  success: "border-success/40 bg-success/10 text-success",
};

/**
 * Small status / category chip. Uppercase + mono-feel for that
 * "label tag in a pro tool" identity.
 */
export function Badge({
  children,
  tone = "neutral",
  className,
}: {
  children: ReactNode;
  tone?: Tone;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider",
        TONE_CLASSES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}
