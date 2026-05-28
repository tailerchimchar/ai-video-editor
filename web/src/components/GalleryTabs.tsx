import { NavLink } from "react-router-dom";
import { cn } from "@/lib/cn";

/**
 * Top-of-page tab switcher for the two main library views: rendered
 * Compilations vs source recordings (Assets). Lives in the PageHeader's
 * `trailing` slot on each page so users can pivot without going back.
 *
 * NavLink handles the active-tab style via the `isActive` callback,
 * which means the currently-open tab gets the accent treatment with
 * no manual route-checking.
 */
export function GalleryTabs() {
  return (
    <nav className="flex items-center gap-1 rounded border border-border bg-bg-base p-1">
      <Tab to="/" label="Compilations" />
      <Tab to="/assets" label="Sources" />
    </nav>
  );
}

interface TabProps {
  to: string;
  label: string;
}

function Tab({ to, label }: TabProps) {
  return (
    <NavLink
      to={to}
      end
      className={({ isActive }) =>
        cn(
          "rounded px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider transition-colors",
          isActive
            ? "bg-bg-elevated text-text-primary"
            : "hover:bg-bg-overlay/40 text-text-muted hover:text-text-primary",
        )
      }
    >
      {label}
    </NavLink>
  );
}
