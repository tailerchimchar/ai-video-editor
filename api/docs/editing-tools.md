# Editing tools (per game)

The living catalogue of every editing tool you can call via MCP — what
it does, what arguments it takes, and (eventually) per-game vocabulary
(named ROIs, sound templates, common moments).

> **Current state.** All editing tools today are **game-agnostic
> primitives**. Per-game profiles (named regions, sound templates,
> game-specific cards) are an *active design* — see
> [§ Per-game profiles (coming)](#per-game-profiles-coming) below and
> the entry in [`roadmap.md`](roadmap.md) (Milestone D). This doc is the
> place the final per-game vocabulary will live once we lock the design.

## Two flavours of editing

| Flavour | When to use | Output |
|---|---|---|
| **Standalone clip edit** (`zoom_clip`, `caption_clip`, `focus_clip`) | One-off edit on a sub-range of an asset, before any compile | New `.mp4` in `WORKSPACE/edits/<asset-stem>/` |
| **Compilation clip edit** (`zoom_compilation_clip`, `focus_compilation_clip`, `caption_compilation_clip`, `extend_compilation_clip`, `remove_compilation_clip`, `set_compilation_labels`, `finalize_compilation`) | Iterating on an already-compiled reel — surgical re-renders of one clip at a time | Updates the compilation's `spec.json` and `compilation.mp4` (only the affected clip is re-encoded; the rest is cached) |

The compilation flow is the headline workflow — it's how you'd build
and polish a real highlight reel.

## How to refer to a clip in a compilation

Every iterative-edit tool takes a `clip_ref` parameter that accepts
**three forms** (tried in this priority order):

1. **1-based index** — `"2"` is the second clip in the reel.
2. **UUID prefix** — the first 4+ chars of the clip's spec id.
3. **Time string** `"M:SS"` (or `"H:MM:SS"`, or plain seconds) — tried
   first as **reel time**, then as **source time** in the original
   recording.

So *"zoom the clip at 0:32"* means "the clip playing 32 seconds into
the reel"; *"zoom the clip at 35:39"* (well past any reel's total
length) falls through to "the source clip whose recording range covers
35:39".

Run **`list_compilation_clips(compilation_id)`** first to see the map.

## Tool catalogue

### Standalone clip editing (game-agnostic)

| Tool | Args | What it does |
|---|---|---|
| `zoom_clip` | `asset_id`, `start_seconds`, `end_seconds`, `factor=2.0`, `roi="center"`, `aspect="16:9"` | Crop to `roi` and upscale by `factor`. `roi` can be a preset name (`"center"`, `"full"`, `"scoreline_lol"`, `"minimap_lol"`) or an explicit dict `{"x","y","w","h"}` (fractions of the source frame). |
| `caption_clip` | `asset_id`, `start_seconds`, `end_seconds`, `text=None`, `aspect="16:9"` | Burn captions. When `text` is omitted, **auto-pulls** the transcript segments overlapping the clip window and times each (TikTok-style auto-captions). |
| `focus_clip` | `asset_id`, `start_seconds`, `end_seconds`, `x=0.5`, `y=0.5`, `radius=0.2`, `dim=0.3`, `aspect="16:9"` | Spotlight: dim the frame to `dim` everywhere except a soft circle at fractional position `(x, y)` of radius `radius * min(w, h)`. |

`aspect="9:16"` triggers a centred crop to 9:16 + scale to 720x1280
(TikTok/Reels). `"16:9"` is a passthrough.

### Compilation building

| Tool | Args | What it does |
|---|---|---|
| `compile_highlights` | `asset_id`, `aspect="16:9"`, `order="chronological"`, `limit=None`, `fade_seconds=0.3`, `music_path=None`, `music_volume=0.25` | Per-clip ffmpeg (effects → captions → fade → aspect tail → label) → concat demuxer → optional music mix. **First render labels every clip `#01`, `#02`…** so you can iterate by position; flip off with `finalize_compilation` (or `set_compilation_labels(enabled=False)`) when done. |
| `list_compilations` | `asset_id=None`, `limit=20` | Find recent rendered compilations (newest first). Returns ids. |
| `list_compilation_clips` | `compilation_id` | Per-clip map: reel & source timestamps, current effects, caption counts. **The thing to call first** before any iterative edit. |

### Compilation iterative editing

| Tool | Args | What it does |
|---|---|---|
| `zoom_compilation_clip` | `compilation_id`, `clip_ref`, `factor=1.5`, `roi="center"` | Add a zoom effect to one clip; re-renders only that clip. |
| `focus_compilation_clip` | `compilation_id`, `clip_ref`, `x=0.5`, `y=0.5`, `radius=0.2`, `dim=0.3` | Add a spotlight effect to one clip. |
| `caption_compilation_clip` | `compilation_id`, `clip_ref`, `text` | Overlay an **extra** caption on top of any auto-pulled transcript. Use for branding moments (`"CLUTCH"`, `"PENTAKILL"`). |
| `extend_compilation_clip` | `compilation_id`, `clip_ref`, `before=0.0`, `after=0.0` | Grow the clip's source window. Seconds before its current start and/or seconds past its current end. |
| `insert_compilation_clip` | `compilation_id`, `asset_id`, `start_seconds`, `end_seconds`, `position=None`, `event_type="manual"`, `text=None` | Add a brand-new clip from any indexed recording's source range. `position` is 1-based; omit for chronological-within-asset. Caption auto-pulls from that asset's transcript unless `text` is supplied. Use when you've found a great moment manually that the ranker missed. |
| `remove_compilation_clip` | `compilation_id`, `clip_ref` | Drop the clip from the reel. The other clips aren't re-rendered (they're cached) so this is near-instant. |
| `set_compilation_labels` | `compilation_id`, `enabled=True` | Toggle the per-clip `#NN` iteration overlay. Cached parts for both label states are kept on disk for instant flipping. |
| `finalize_compilation` | `compilation_id` | Sugar for `set_compilation_labels(enabled=False)` — reads as "render final" for end-of-iteration. |

### Adjacent: transcript & search

These aren't editing tools but they power what you put in your reel.

| Tool | What it does |
|---|---|
| `transcribe_asset` | Local Whisper STT (CPU int8, private, $0). Stored once and reused by candidate generation. |
| `get_transcript` | Fetch the stored transcript segments for an asset. |
| `index_asset` | Embed the transcript into the vector store ($0, local). |
| `search_clips` | Natural-language semantic search: *"find clips where I clutched a 1v3"*, *"funny reactions"*. Returns matched clips with reel/source timestamps + distance. |

## Per-game profiles

A profile is a TOML file that names a game's editing **vocabulary** —
ROI regions (as fractions of the source frame) and expected sound
file names. Profiles ship with the repo at
`src/ai_video_editor/profiles/<game>.toml`. The user-supplied audio
templates live separately in the workspace at
`WORKSPACE/media_library/<game>/sfx/` (copyrighted audio can't be
versioned).

Lookup is by `asset.game` matched case-insensitively against each
profile's `meta.game` and `meta.aliases`. Unknown games fall back to
the `default` profile (empty vocab) so generic primitives keep
working without crashing.

### Schema

```toml
[meta]
game = "league"                       # canonical key
aliases = ["League of Legends", "lol"]  # asset.game values that match
default_aspect = "16:9"               # "16:9" or "9:16"

[regions.minimap]
x = 0.85; y = 0.72; w = 0.15; h = 0.28   # fractions [0,1], validated
description = "bottom-right rotation map"

[sounds.first_blood]
file = "first_blood.wav"              # basename under media_library/<game>/sfx/
description = "Announcer for the opening kill"
```

`Region` validates each coord in `[0,1]` and rejects boxes that
extend past the frame edge (`x+w > 1` or `y+h > 1`).

### v1 LoL vocab — `profiles/league.toml`

| Regions | Sounds |
|---|---|
| `minimap`, `scoreboard`, `tab_overlay`, `killfeed`, `champion_portrait`, `item_bar`, `hp_mana` | `first_blood`, `enemy_slain`, `ace`, `penta_kill`, `baron_slain`, `buy_item` |

### v1 Valorant vocab — `profiles/valorant.toml`

| Regions | Sounds |
|---|---|
| `minimap`, `scoreboard`, `tab_overlay`, `killfeed`, `crosshair`, `agent_card`, `ult_orbs`, `money_display` | `cash_pickup`, `headshot_ping`, `kill_confirmed`, `ace`, `spike_planted`, `spike_defused` |

Region coordinates are starting guesses for a 1920×1080 capture at
default HUD scale. **Calibrate against a real recording** before
sprint #3's `zoom_region` relies on them — HUD scale, windowed mode,
and OBS resolution all shift the layout. The TOML is the single
place to edit; nothing else changes.

### Sourcing SFX files — `POST /sfx/extract`

The profile *names* sounds; you supply the audio. The easiest path is
to extract clean spans from a reference recording:

```bash
curl -X POST localhost:8000/api/v1/sfx/extract \
  -H "Content-Type: application/json" \
  -d '{
    "asset_id": "<asset-id>",
    "game": "league",
    "sound_name": "first_blood",
    "start_seconds": 184.5,
    "end_seconds": 186.5
  }'
# -> {"job_id": "..."}    poll /api/v1/jobs/{id}
```

Output lands at `WORKSPACE/media_library/<canonical-game>/sfx/<sound-file>.wav`
(mono 44.1 kHz PCM — the shape sprint #4's mel-spectrogram matcher
expects). Aliases (`"League of Legends"`, `"lol"`) all resolve to the
same `league/` directory.

If you extract a `sound_name` that isn't declared in the profile, the
file is still saved (as `<sound_name>.wav`) but the job warns you to
add the entry to the profile so downstream tools pick it up.

### Plumbing in code

```python
from ai_video_editor.profiles import load_profile, region_box, sound_path

profile = load_profile(asset["game"])      # falls back to 'default'
box = region_box("league", "minimap")      # Region | None
wav = sound_path("league", "first_blood")  # Path | None (None if file missing)
```

`region_box` and `sound_path` are the API sprint #3's `zoom_region`,
`add_sfx`, and the auto-generated `league_*` / `valorant_*` MCP
wrappers will call.

### Coming in sprint #3 — primitives that consume the profile

```
zoom_region(clip_ref, region_name)              # "minimap", "killfeed", ...
add_sfx(clip_ref, anchor_seconds, sound_name)   # "first_blood", "ace", ...
add_card(position, text, duration, sfx?)        # interstitial between clips
```

Plus auto-generated per-game wrappers (`league_zoom_minimap`,
`valorant_play_headshot`, …) derived from each profile's `regions`
and `sounds` sections. The compilation's `asset.game` resolves which
profile is in scope, so the same call works across LoL and Valorant —
only the underlying ROI / audio file differs.

## Output locations

```
WORKSPACE/
├── edits/<asset-stem>/{zoom,caption,focus}_<timestamp>.mp4
└── compilations/<asset-stem>_<timestamp>/
    ├── spec.json           ← mutable source of truth
    ├── compilation.mp4     ← regenerated from spec
    ├── index.json          ← summary of last render
    └── _parts/
        ├── part_<id>.mp4         ← cached per-clip render
        └── part_<id>_n3.mp4      ← labelled variant (#03)
```

The compilation `_parts/` directory makes iteration fast: an effect
change re-encodes **one** part file; concat is stream-copy.

## How to extend this catalogue

1. **A new effect** → add an `effects` entry the spec accepts, extend
   `_build_clip_filterchain` in `compile.py`, add the matching standalone
   in `edits.py` if useful, expose via MCP. (Tests live in
   `tests/test_compile.py` and `tests/test_edits.py`.)
2. **A new ROI preset** → add to `_ROI_PRESETS` in `edits.py`. Keep
   coordinates as fractions of the source dimensions (resolution-
   independent).
3. **A new game profile** → *(once the design lands)* add a profile
   entry; named regions / sound templates / cards live here, not in
   the primitives.