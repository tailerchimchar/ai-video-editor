# Jev clip cut (ffmpeg signals + criteria)

Optional replacement for Haiku-suggested clip in/out windows. Default
**off**. When on, highlight cutting proposes a few (start, end) options
from free local ffmpeg signals (silence, loudness, scene changes), asks
Jev one batched criteria check, and picks the option that passes the
most criteria confidently. Never calls Haiku for this step.

## Flags

| Setting | Env | Default | Meaning |
|---|---|---|---|
| `grokbot_jev_clip_cut_enabled` | `GROKBOT_JEV_CLIP_CUT_ENABLED` | `false` | Use the Jev-picked window when cutting highlights. |
| `grokbot_jev_clip_cut_shadow` | `GROKBOT_JEV_CLIP_CUT_SHADOW` | `false` | Compute the Jev cut alongside the existing window, **keep the existing result**, and log one comparison line per clip. |

Both may be on together (shadow logs while enabled also applies the pick).
With both off, behavior is unchanged.

Requires `TYPESAFE_API_KEY` when the path actually asks Jev. Missing key /
Jev errors / rules-backed answers → fall back to event time ± current
padding and mark `flagged=True` (fail safe).

## Wired where

`highlights.build_highlights` → after `cut_window(...)`, via
`_apply_jev_clip_cut`. Smallest possible hook at the single place clip
timing is decided before ffmpeg trim / VLM.

## Shadow comparison (~20 clips)

1. Set `GROKBOT_JEV_CLIP_CUT_SHADOW=true` (leave enabled false).
2. Ensure `TYPESAFE_API_KEY` is set so Jev answers (otherwise every clip
   flags and the deltas are just padding vs legacy).
3. Cut highlights for a recording that yields ~20 kept clips
   (`POST /assets/{id}/highlights` after rank).
4. Read the API log:

### Per-clip shadow line

```
jev_clip_cut shadow event=kill delta_start=-0.40 delta_end=1.20 criteria_passed=4 flagged=False jev_ms=85 input_tokens=1200 label=quiet_snap
```

- `delta_start` / `delta_end` — Jev pick minus the legacy `cut_window` result (seconds).
- `criteria_passed` — how many criteria that option passed confidently.
- `flagged` — true when the picker fell back to event±padding.
- `jev_ms` / `input_tokens` — that clip's Jev ask cost.

### Per-run summary line

```
jev_clip_cut run clips=20 jev_requests=1 flagged=3 fallback=3 skipped_no_anchor=2 ffmpeg_ms=4100 jev_ms=1800 input_tokens=24000
```

- `jev_requests` — usually **1** for the whole highlight run (chunked only
  if the soft question ceiling is hit).
- `skipped_no_anchor` — clips with no real event time (`metadata.anchor_seconds`);
  fuzzy sources like `audio_peak` / `transcript_keyword` / `outplayed_clip`.
  Those keep the legacy window and never invent a midpoint.

No separate cost-log helper on master — these lines are the observability
surface. ffmpeg work is local ($0); Jev bills input tokens only.
