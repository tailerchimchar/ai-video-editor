import type { Config } from "tailwindcss";

// Theme tokens live in `src/styles/tokens.css` as CSS variables. Tailwind
// reads them via the `var()` references below, so the dev workflow is:
// edit a token → every component using it re-themes. Adding a new color
// here means adding both the CSS var AND the alias below.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: {
          base: "var(--bg-base)",
          elevated: "var(--bg-elevated)",
          overlay: "var(--bg-overlay)",
        },
        border: {
          DEFAULT: "var(--border)",
          strong: "var(--border-strong)",
        },
        text: {
          primary: "var(--text-primary)",
          muted: "var(--text-muted)",
          dim: "var(--text-dim)",
        },
        accent: {
          DEFAULT: "var(--accent)",
          glow: "var(--accent-glow)",
        },
        danger: "var(--danger)",
        success: "var(--success)",
      },
      fontFamily: {
        // Distinctive choices per the frontend-design skill:
        // serif for identity, sans for UI, mono for backend-facing values.
        display: ["'Instrument Serif'", "ui-serif", "Georgia", "serif"],
        sans: ["'Geist Variable'", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["'JetBrains Mono Variable'", "ui-monospace", "Menlo", "monospace"],
      },
      borderRadius: {
        DEFAULT: "4px",
        sm: "2px",
        md: "6px",
        lg: "8px",
      },
    },
  },
  plugins: [],
} satisfies Config;
