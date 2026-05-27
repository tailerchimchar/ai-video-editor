import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Compose Tailwind class strings with proper precedence.
 *
 *   cn("p-4", condition && "p-6", className)
 *
 * `clsx` filters falsy values; `twMerge` resolves Tailwind conflicts
 * (later wins) so component consumers can override defaults without
 * cascade fights.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
