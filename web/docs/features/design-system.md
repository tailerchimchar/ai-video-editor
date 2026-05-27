# Design system

Refined cinematic minimalism. Dark, distinctive, no generic AI-
template aesthetic. Built by the rules in the
[Anthropic frontend-design skill](https://github.com/anthropics/skills/blob/main/skills/frontend-design/SKILL.md).

## Typography

Three families. Each has ONE job; mixing them creates the hierarchy.

| Role | Font | Used for |
|---|---|---|
| **Display** | Instrument Serif | Page titles, "Compilation" hero, big identity moments |
| **Body / UI** | Geist Variable | Everything sans — buttons, labels, body copy |
| **Mono** | JetBrains Mono Variable | Timestamps, IDs, file paths, any value that maps to a backend type |

Rules enforced by component patterns:
- **Serif = identity.** Used sparingly — only the H1 on each page.
- **Sans = UI default.** All buttons, labels, paragraph text.
- **Mono = "this is a backend value."** Clip ids (`6ddc161e`),
  timestamps (`0:42 → 0:55`), durations (`30.00s`), file paths.
  Reinforces "this is a craft tool" identity.

Fonts are self-hosted via `@fontsource` packages so the first paint
already has the right family — no FOUT.

## Colour palette

Single dominant surface + single sharp accent. No gradients in the
base palette (depth comes from the grain texture + 1px inset
highlights on elevated cards).

Defined as CSS variables in `src/styles/tokens.css`:

```css
:root {
  /* Surfaces */
  --bg-base:     #0a0a0b;      /* page background — near-black */
  --bg-elevated: #141416;      /* cards, panels */
  --bg-overlay:  #1c1c20;      /* hover surfaces */

  /* Borders */
  --border:        #26262a;    /* hairline dividers */
  --border-strong: #3a3a40;    /* tracks, slider rails */

  /* Text */
  --text-primary: #fafafa;
  --text-muted:   #8a8a90;
  --text-dim:     #52525a;

  /* Brand accent */
  --accent:      #3b82f6;      /* Noodlz electric blue */
  --accent-glow: rgba(59, 130, 246, 0.18);

  /* Status */
  --danger:  #ef4444;
  --success: #22c55e;
}
```

Tailwind aliases these as `bg-base`, `text-primary`, `accent`,
`danger` etc. — see `tailwind.config.ts`.

**No purple gradients. No dual gradients. No light theme yet.** The
single accent is the brand — adding a second hue dilutes it.

## Spacing + layout

- **8px grid.** Tailwind defaults (1 = 4px) — most spacing is
  `2 4 6 8 12 16 24`.
- **Asymmetric layouts.** The viewer is 7/5 (not 50/50). The history
  rail sits at the bottom of the right column (not in a separate
  third column). Predictable grid layouts are forbidden by the
  design skill.
- **Generous negative space.** Cards have `p-6` minimum inner
  padding. Outer gaps between sections feel quiet.
- **Hairline borders, not boxy ones.** 1px `--border`, no
  `border-radius` over `8px` (refined, not bubble-y).
- **No drop shadows on cards.** Use `inset 0 1px 0 #ffffff08`
  top-edge highlight instead (the Apple Pro / Linear move). See
  `.surface-elevated` in `globals.css`.

## Motion

- **One orchestrated page load > many scattered animations.**
  ClipFilmstrip + CompilationsList use 60ms stagger fade-in on
  mount. No tween elsewhere unless it carries meaning.
- **Hover = subtle weight shift.** ~2% scale on filmstrip tiles,
  border brightening on rows. No jumpy translates.
- **Slider drag = direct manipulation** with no easing — handles
  feel like physical controls.
- **`prefers-reduced-motion` respected.** Hard requirement. Strips
  entry animations and transitions via a `@media` block in
  `globals.css`.

## Visual character

- **Film-grain texture** as a fixed-position pseudo-element with
  `mix-blend-mode: screen` and 4% opacity. Adds the cinematic
  depth the skill calls for without competing with UI elements.
- **Mono-typed metadata everywhere** — `#3b82f6` next to color
  tokens, `7.32s` next to durations, `id 6ddc161e` next to ids.

## Tokens → Tailwind utility flow

```
tokens.css         (CSS variables — source of truth)
       │
       ▼
tailwind.config.ts (alias `bg-base`, `text-primary`, `accent`, etc.)
       │
       ▼
components         use `className="bg-base text-primary border-border"`
```

Add a new colour:
1. Add CSS variable to `tokens.css`.
2. Add an alias under `theme.extend.colors` in `tailwind.config.ts`.
3. Use the Tailwind class throughout components.

This way theme swapping is one rule away (e.g., light theme would
swap the `:root` variable values; nothing else changes).

## Components inventory

Primitives live in `src/components/ui/`:

- `Button` — three variants (`primary`, `ghost`, `danger`), two
  sizes. Mono uppercase labels for the craft-tool look.
- `Badge` — small uppercase tag, five tones (`neutral`, `accent`,
  `muted`, `danger`, `success`).
- `PageHeader` — serif title + mono subtitle + trailing slot.
  Identity row at the top of every page.
- `PageShell` — outer wrapper providing the responsive max-width
  + outer padding.

Domain components live one level up in `src/components/`:

- `VideoPlayer`, `ClipFilmstrip`, `ClipMetaPanel`, `ClipActionsPanel`,
  `CaptionEditor`, `ExtendSlider`, `HistoryRail`, `CompilationRow`

## What this is NOT

- **shadcn/ui.** We don't use the CLI. We hand-write the few
  primitives we need. shadcn-style copy-paste is fine; the
  dependency isn't.
- **A theme system.** Just one (dark) theme right now. CSS variables
  mean light is a swap-out, but it's not built.
- **A component library.** Components are project-specific. If
  something starts looking like a generic primitive, it gets moved
  to `ui/` but stays in this repo.
