"""MCP server for the ai-video-editor backend.

Thin stdio adapter over the HTTP API — every tool is a wrapper over an
existing endpoint. No backend logic lives here. Job-based steps poll
internally so one tool call = one completed step.

This server lives in its own repo (`ai-video-editor-mcp`) so it can
evolve independently of the backend and be installed without dragging
in FastAPI, sqlite, faster-whisper, etc. The backend is reached via
`AI_VIDEO_EDITOR_URL` (default http://localhost:8000).

Run: `uv run ai-video-editor-mcp` (stdio).
"""

import os
import time

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("AI_VIDEO_EDITOR_URL", "http://localhost:8000").rstrip("/")
API = f"{BASE_URL}/api/v1"
_JOB_TIMEOUT_S = 300

mcp = FastMCP("ai-video-editor")


def _client() -> httpx.Client:
    return httpx.Client(timeout=30.0)


def _wait_for_job(client: httpx.Client, job_id: str) -> dict:
    """Poll a background job until it finishes (or times out)."""
    deadline = time.monotonic() + _JOB_TIMEOUT_S
    while time.monotonic() < deadline:
        job = client.get(f"{API}/jobs/{job_id}").json()
        if job.get("status") in ("completed", "failed"):
            return job
        time.sleep(2)
    return {"status": "timeout", "error": f"job {job_id} did not finish in {_JOB_TIMEOUT_S}s"}


@mcp.tool()
def scan_assets() -> dict:
    """Scan the configured Outplayed folder and index new .mp4 recordings.
    Run this once after new recordings appear. Free, local, instant."""
    with _client() as c:
        return c.post(f"{API}/assets/scan").json()


@mcp.tool()
def list_assets(game: str | None = None, limit: int = 20) -> list[dict]:
    """List indexed recordings (newest first). Optionally filter by game
    substring, e.g. 'League' or 'Valorant'. Returns id/filename/game."""
    with _client() as c:
        assets = c.get(f"{API}/assets").json()
    if game:
        g = game.lower()
        assets = [a for a in assets if g in f"{a.get('game') or ''} {a['filename']}".lower()]
    assets.sort(key=lambda a: a["created_at"], reverse=True)
    return [
        {
            "id": a["id"],
            "filename": a["filename"],
            "game": a.get("game"),
            "created_at": a["created_at"],
        }
        for a in assets[:limit]
    ]


@mcp.tool()
def generate_candidates(asset_id: str) -> dict:
    """Generate cheap highlight candidates for one recording (Outplayed
    clip / Riot kills / audio peaks). Free, no LLM. Waits for completion
    and returns a per-source count."""
    with _client() as c:
        job_id = c.post(f"{API}/assets/{asset_id}/candidates").json()["job_id"]
        job = _wait_for_job(c, job_id)
        if job.get("status") != "completed":
            return {"status": job.get("status"), "error": job.get("error")}
        cands = c.get(f"{API}/assets/{asset_id}/candidates").json()
    by_source: dict[str, int] = {}
    for x in cands:
        by_source[x["source"]] = by_source.get(x["source"], 0) + 1
    return {"status": "completed", "total": len(cands), "by_source": by_source}


@mcp.tool()
def list_candidates(asset_id: str) -> list[dict]:
    """List the raw (un-ranked) highlight candidates for a recording."""
    with _client() as c:
        return c.get(f"{API}/assets/{asset_id}/candidates").json()


@mcp.tool()
def transcribe_asset(asset_id: str) -> dict:
    """Transcribe one recording with local Whisper (private, $0). Waits
    for the job and returns the segment count + a short preview. Short
    clips finish in seconds; long VODs are slow on CPU and may report
    'timeout' here while still running — re-check with get_transcript /
    get_job. Stored once, then reused by candidate generation."""
    with _client() as c:
        job_id = c.post(f"{API}/assets/{asset_id}/transcribe").json()["job_id"]
        job = _wait_for_job(c, job_id)
        if job.get("status") != "completed":
            return {"status": job.get("status"), "note": job.get("error") or job.get("output_path")}
        segs = c.get(f"{API}/assets/{asset_id}/transcript").json()
    return {
        "status": "completed",
        "segments": len(segs),
        "preview": [s["text"] for s in segs[:5]],
    }


@mcp.tool()
def get_transcript(asset_id: str) -> list[dict]:
    """Return the stored transcript (start/end/text segments) for a
    recording, if it has been transcribed."""
    with _client() as c:
        return c.get(f"{API}/assets/{asset_id}/transcript").json()


@mcp.tool()
def rank_asset(asset_id: str) -> list[dict]:
    """Run the LLM ranker on a recording's candidates (one Claude call,
    ~$0.005). Waits for completion and returns kept suggestions sorted by
    hype score. Requires candidates to have been generated first."""
    with _client() as c:
        job_id = c.post(f"{API}/assets/{asset_id}/rank").json()["job_id"]
        job = _wait_for_job(c, job_id)
        if job.get("status") != "completed":
            return [{"status": job.get("status"), "error": job.get("error")}]
        ranked = c.get(f"{API}/assets/{asset_id}/rankings").json()
    kept = [r for r in ranked if r.get("keep")]
    kept.sort(key=lambda r: r.get("hype_score", 0), reverse=True)
    return kept


@mcp.tool()
def cut_highlights(asset_id: str) -> dict:
    """Cut every kept ranked suggestion into an organized, browsable
    folder: highlights/<game>/<date>_<champion>/NN_event_time.mp4 plus an
    index. Requires the asset to have been ranked first. Waits for the
    job and returns the folder path + per-clip results."""
    with _client() as c:
        job_id = c.post(f"{API}/assets/{asset_id}/highlights").json()["job_id"]
        job = _wait_for_job(c, job_id)
        if job.get("status") != "completed":
            return {"status": job.get("status"), "error": job.get("error")}
        return c.get(f"{API}/assets/{asset_id}/highlights").json()


@mcp.tool()
def batch_clip_highlights(game: str, limit: int = 30) -> dict:
    """Organize a game's short Outplayed event clips (e.g. game='Valorant')
    into highlights/<game>/clips_<date>/. No LLM, $0 — Outplayed already
    curated these, so it just copies them newest-first with an index.
    Best for Valorant (no Riot kill data) and clearing 'tons of clips'.
    Waits for the job and returns the folder + per-clip results."""
    with _client() as c:
        resp = c.post(f"{API}/clips/batch-highlights", params={"game": game, "limit": limit})
        job_id = resp.json()["job_id"]
        job = _wait_for_job(c, job_id)
    return job if job.get("status") != "completed" else {"summary": job.get("output_path")}


@mcp.tool()
def analyze_asset(asset_id: str, cut: bool = False) -> dict:
    """One-shot: generate candidates AND rank them for a recording. If
    cut=True, also cut the kept suggestions into the organized highlights
    folder. The simplest entry point — 'analyze this video' end to end."""
    gen = generate_candidates(asset_id)
    if gen.get("status") != "completed":
        return gen
    kept = rank_asset(asset_id)
    if cut:
        return {"ranked": kept, "highlights": cut_highlights(asset_id)}
    return {"ranked": kept}


@mcp.tool()
def get_job(job_id: str) -> dict:
    """Check the status of any background job by id."""
    with _client() as c:
        return c.get(f"{API}/jobs/{job_id}").json()


@mcp.tool()
def index_asset(asset_id: str) -> dict:
    """Embed this recording's transcript into the vector store so it
    becomes searchable. Requires transcribe_asset to have been run first.
    Local embeddings, $0. Waits for the job to finish."""
    with _client() as c:
        job_id = c.post(f"{API}/assets/{asset_id}/index").json()["job_id"]
        job = _wait_for_job(c, job_id)
    return {"status": job.get("status"), "summary": job.get("output_path") or job.get("error")}


@mcp.tool()
def search_clips(query: str, limit: int = 10, asset_id: str | None = None) -> list[dict]:
    """Natural-language semantic search across indexed transcripts:
    "find clips where I clutched a 1v3", "show me funny reactions", etc.
    Returns matched clips (asset, time range, text, distance — lower is
    closer). Pass asset_id to restrict to one recording."""
    with _client() as c:
        params: dict[str, object] = {"q": query, "limit": limit}
        if asset_id:
            params["asset_id"] = asset_id
        return c.get(f"{API}/search", params=params).json()


def _edit(kind: str, payload: dict) -> dict:
    """Shared helper for the edit tools — POST and wait for the job."""
    with _client() as c:
        r = c.post(f"{API}/edit/{kind}", json=payload).json()
        job_id = r["job_id"]
        job = _wait_for_job(c, job_id)
    return {"status": job.get("status"), "output": job.get("output_path") or job.get("error")}


@mcp.tool()
def zoom_clip(
    asset_id: str,
    start_seconds: float,
    end_seconds: float,
    factor: float = 2.0,
    roi: str = "center",
    aspect: str = "16:9",
) -> dict:
    """Zoom in on part of a clip's frame. `roi` can be 'center',
    'scoreline_lol', 'minimap_lol', or 'full'. `aspect` is '16:9' or
    '9:16' (TikTok/Reels vertical). Renders a new .mp4 to
    WORKSPACE/edits/."""
    return _edit(
        "zoom",
        {
            "asset_id": asset_id,
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
            "factor": factor,
            "roi": roi,
            "aspect": aspect,
        },
    )


@mcp.tool()
def caption_clip(
    asset_id: str,
    start_seconds: float,
    end_seconds: float,
    text: str | None = None,
    aspect: str = "16:9",
) -> dict:
    """Burn timestamped captions onto a clip. When `text` is omitted,
    pulls the transcript segments overlapping [start, end] from the DB
    and draws each at its real time (TikTok-style auto-captions).
    Requires the asset to have been transcribed first if you want the
    auto path."""
    return _edit(
        "caption",
        {
            "asset_id": asset_id,
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
            "text": text,
            "aspect": aspect,
        },
    )


@mcp.tool()
def focus_clip(
    asset_id: str,
    start_seconds: float,
    end_seconds: float,
    x: float = 0.5,
    y: float = 0.5,
    radius: float = 0.2,
    dim: float = 0.3,
    aspect: str = "16:9",
) -> dict:
    """Spotlight effect: darken the frame and leave a soft circle of
    normal brightness at fractional position (x, y) with radius
    `radius * min(w, h)`. `dim` is the outside-circle brightness (0..1)."""
    return _edit(
        "focus",
        {
            "asset_id": asset_id,
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
            "x": x,
            "y": y,
            "radius": radius,
            "dim": dim,
            "aspect": aspect,
        },
    )


@mcp.tool()
def compile_highlights(
    asset_id: str,
    aspect: str = "16:9",
    order: str = "hook",
    limit: int | None = None,
    fade_seconds: float = 0.3,
    music_path: str | None = None,
    music_volume: float = 0.25,
) -> dict:
    """Stitch a recording's kept ranked highlights into ONE polished
    .mp4: per-clip auto-captions + fade-in/out + optional background
    music mixed under the source audio.

    `order` modes:
    - `"hook"` *(default)* — highest-hype clip plays FIRST, the rest
      in chronological order. Maximises first-3-seconds retention
      (the algorithm signal that decides if your video gets pushed).
    - `"hype"` — all clips sorted by hype score descending. Best clip
      first but every following clip is also hype-ordered.
    - `"chronological"` — story order, what happened first plays first.
      Use when context matters more than retention.
    - `"narrative"` — three sections in recording order (intro/main/outro).
      Intro pulls 1-2 top-hype moments from the first 10 min of the
      recording (warmup/greeting), main is the chronological body, outro
      pulls 1 top moment from the last 10 min (post-game commentary).
      Best for long Twitch VODs that benefit from a story arc.

    Requires the asset to have been transcribed AND ranked first.
    Output goes to WORKSPACE/compilations. Long jobs may return
    'timeout' here while still running."""
    payload = {
        "asset_id": asset_id,
        "aspect": aspect,
        "order": order,
        "fade_seconds": fade_seconds,
        "music_volume": music_volume,
    }
    if limit is not None:
        payload["limit"] = limit
    if music_path is not None:
        payload["music_path"] = music_path
    with _client() as c:
        job_id = c.post(f"{API}/edit/compile", json=payload).json()["job_id"]
        job = _wait_for_job(c, job_id)
    return {"status": job.get("status"), "summary": job.get("output_path") or job.get("error")}


# --- Iterative compilation editing (Phase 3, B+) ---


@mcp.tool()
def list_compilations(asset_id: str | None = None, limit: int = 20) -> list[dict]:
    """List recent rendered compilations (newest first) so you can find
    a `compilation_id` to iterate on. Optional asset_id filter."""
    with _client() as c:
        params: dict[str, object] = {"limit": limit}
        if asset_id:
            params["asset_id"] = asset_id
        return c.get(f"{API}/edit/compile", params=params).json()


@mcp.tool()
def list_compilation_clips(compilation_id: str) -> dict:
    """Show the clips in a rendered compilation with reel + source
    timestamps, current effects, and caption counts — the map you need
    before editing by reel time ('zoom the clip at 0:32')."""
    with _client() as c:
        return c.get(f"{API}/edit/compile/{compilation_id}/clips").json()


def _compile_edit(compilation_id: str, op: str, body: dict) -> dict:
    with _client() as c:
        return c.post(f"{API}/edit/compile/{compilation_id}/{op}", json=body).json()


@mcp.tool()
def zoom_compilation_clip(
    compilation_id: str,
    clip_ref: str,
    factor: float = 1.5,
    roi: str = "center",
) -> dict:
    """Add a zoom effect to one clip in a compilation and re-render it.
    `clip_ref` accepts a 1-based index ('2'), a UUID prefix, or a time
    string ('0:32' tries reel time first, then source time)."""
    return _compile_edit(
        compilation_id, "effect",
        {"clip_ref": clip_ref, "kind": "zoom", "factor": factor, "roi": roi},
    )


@mcp.tool()
def focus_compilation_clip(
    compilation_id: str,
    clip_ref: str,
    x: float = 0.5,
    y: float = 0.5,
    radius: float = 0.2,
    dim: float = 0.3,
) -> dict:
    """Add a spotlight (darken-everywhere-except-circle) effect to one
    clip in a compilation. Positions are fractions of frame size."""
    return _compile_edit(
        compilation_id, "effect",
        {"clip_ref": clip_ref, "kind": "focus", "x": x, "y": y,
         "radius": radius, "dim": dim},
    )


@mcp.tool()
def caption_compilation_clip(compilation_id: str, clip_ref: str, text: str) -> dict:
    """Overlay an extra caption (on top of any auto-pulled transcript)
    onto one clip in the compilation. Use this to brand/title a moment
    like 'CLUTCH' or 'PENTAKILL'."""
    return _compile_edit(
        compilation_id, "effect",
        {"clip_ref": clip_ref, "kind": "caption", "text": text},
    )


@mcp.tool()
def extend_compilation_clip(
    compilation_id: str, clip_ref: str, before: float = 0.0, after: float = 0.0
) -> dict:
    """Grow a clip's source window (seconds before its current start
    and/or seconds past its current end). The clip gets re-rendered;
    everything else stays cached."""
    return _compile_edit(
        compilation_id, "extend",
        {"clip_ref": clip_ref, "before": before, "after": after},
    )


@mcp.tool()
def insert_compilation_clip(
    compilation_id: str,
    asset_id: str,
    start_seconds: float,
    end_seconds: float,
    position: int | None = None,
    event_type: str = "manual",
    text: str | None = None,
) -> dict:
    """Insert a NEW clip into an existing compilation from any indexed
    recording's source range. Use this when you've found a great moment
    manually (transcript search, gut feel) that the ranker missed.

    `position` is 1-based; omit to insert chronologically by source time
    within the same asset (the typical case — one recording per reel).
    `event_type` shows up in the per-clip summary; default "manual".
    `text` overrides the caption — omit to auto-pull transcript segments
    overlapping [start, end] from the asset's stored transcript.
    """
    body: dict = {
        "asset_id": asset_id,
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
        "event_type": event_type,
    }
    if position is not None:
        body["position"] = position
    if text is not None:
        body["text"] = text
    return _compile_edit(compilation_id, "insert", body)


@mcp.tool()
def remove_compilation_clip(compilation_id: str, clip_ref: str) -> dict:
    """Drop a clip from a compilation and re-concat. The other clips
    aren't re-rendered (they're cached) so this is near-instant."""
    return _compile_edit(compilation_id, "remove", {"clip_ref": clip_ref})


@mcp.tool()
def set_compilation_labels(compilation_id: str, enabled: bool = True) -> dict:
    """Toggle the iteration '#NN' label overlay on every clip in a
    compilation. ON = labeled for editing ('zoom #04'); OFF = clean
    final render. Re-renders all parts (one filter chain changed) but
    cached parts for the opposite label state are kept on disk for
    fast flipping back."""
    return _compile_edit(compilation_id, "labels", {"enabled": enabled})


@mcp.tool()
def finalize_compilation(compilation_id: str) -> dict:
    """Render a clean final version of a compilation (labels off).
    Equivalent to `set_compilation_labels(compilation_id, enabled=False)`
    — but reads more naturally as a 'render final' action."""
    return _compile_edit(compilation_id, "labels", {"enabled": False})


@mcp.tool()
def extract_sfx(
    asset_id: str,
    game: str,
    sound_name: str,
    start_seconds: float,
    end_seconds: float,
) -> dict:
    """Cut an audio span out of a recording into the per-game SFX library.

    Use this to source the announcer/cue templates a profile names —
    e.g. extract 'first_blood' from a LoL VOD where you hear the
    announcer say it, and the file lands at
    `WORKSPACE/media_library/league/sfx/first_blood.wav` (mono 44.1 kHz).

    `game` accepts any profile alias ('league', 'League of Legends',
    'lol' all resolve to the same directory). If `sound_name` isn't
    declared in the profile, the file is still saved but the job
    output flags a warning so you know to add the entry.
    """
    with _client() as c:
        job_id = c.post(
            f"{API}/sfx/extract",
            json={
                "asset_id": asset_id,
                "game": game,
                "sound_name": sound_name,
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
            },
        ).json()["job_id"]
        return _wait_for_job(c, job_id)


@mcp.tool()
def tiktok_caption_clip(compilation_id: str, clip_ref: str) -> dict:
    """Switch ONE clip in a rendered compilation to TikTok-style
    word-by-word captions (top-center, big bold white text with black
    border, one word visible at a time synced to the speaker).

    Requires the source transcript to have word-level timestamps —
    produced by `transcribe_asset` (the transcribe pipeline now
    always requests `word_timestamps=True`). Legacy transcripts
    without per-word timing fall back to even-split timing across
    each segment's duration.

    Only this clip re-renders. Use `segment_caption_clip` to revert.
    """
    return _compile_edit(
        compilation_id, "caption_mode", {"clip_ref": clip_ref, "mode": "tiktok"}
    )


@mcp.tool()
def segment_caption_clip(compilation_id: str, clip_ref: str) -> dict:
    """Revert a clip's captions back to the default segment style
    (one bottom-center line per Whisper segment). Companion to
    `tiktok_caption_clip`."""
    return _compile_edit(
        compilation_id, "caption_mode", {"clip_ref": clip_ref, "mode": "segment"}
    )


@mcp.tool()
def create_intro(
    name: str,
    logo_source_path: str | None = None,
    preset: str | None = None,
    music_source_path: str | None = None,
    duration: float | None = None,
    background_color: str | None = None,
    logo_scale: float | None = None,
    bounce_pixels: int | None = None,
    bounce_count: int | None = None,
    fade_in_seconds: float | None = None,
    pulse_factor: float | None = None,
) -> dict:
    """Create and render a NEW image-mode intro from a logo PNG.

    Provide EITHER `logo_source_path` (path on disk) OR `preset`
    (name from `list_intro_presets`). Cannot supply both.

    The intro is a small (~3s) mp4 you prepend to compilations — your
    Noodlz logo with a damped bounce animation. Re-usable across
    every reel. Lives under `WORKSPACE/intros/<name>/`.

    `logo_source_path` is COPIED into the intro folder (not symlinked)
    so the intro is self-contained / portable. Same for music.

    Defaults work out of the box; tweak `logo_scale` (0..1 of frame
    width), `bounce_pixels` (vertical bounce amplitude), or
    `duration` after seeing v1 if it doesn't feel right. Use
    `update_intro` to iterate without re-creating.

    For text-only intros (no PNG, fully procedural), see
    `create_text_intro`.

    Returns the render summary (`output`, `duration`, `ok`,
    `error`) plus the resolved config.
    """
    body: dict = {"name": name}
    if logo_source_path is not None:
        body["logo_source_path"] = logo_source_path
    if preset is not None:
        body["preset"] = preset
    if music_source_path is not None:
        body["music_source_path"] = music_source_path
    for k, v in (
        ("duration", duration),
        ("background_color", background_color),
        ("logo_scale", logo_scale),
        ("bounce_pixels", bounce_pixels),
        ("bounce_count", bounce_count),
        ("fade_in_seconds", fade_in_seconds),
        ("pulse_factor", pulse_factor),
    ):
        if v is not None:
            body[k] = v
    with _client() as c:
        return c.post(f"{API}/intros", json=body).json()


@mcp.tool()
def create_text_intro(
    name: str,
    text: str,
    font_path: str = "",
    font_size: int = 200,
    font_color: str = "white",
    stroke_width: int = 0,
    stroke_color: str = "black",
    letter_spacing: int = 0,
    alignment: str = "center",
    shadow_offset_x: int = 0,
    shadow_offset_y: int = 0,
    shadow_color: str = "black",
    shadow_alpha: float = 0.5,
    duration: float | None = None,
    background_color: str | None = None,
    bounce_pixels: int | None = None,
    bounce_count: int | None = None,
    fade_in_seconds: float | None = None,
    music_source_path: str | None = None,
    music_volume: float | None = None,
) -> dict:
    """Create and render a text-mode intro (NO PNG required).

    The text is rendered procedurally via per-letter `drawtext`, so
    every visual property is config-driven and iterable:
    - `text`: the brand string ("NOODLZ", anything)
    - `font_path`: TTF/OTF path (falls back to system default)
    - `font_size`, `font_color`: basic typography
    - `stroke_width`, `stroke_color`: outline around the letters
    - `letter_spacing`: extra px between letters (negative = tighter)
    - `alignment`: `"left" | "center" | "right"`
    - `shadow_offset_x/y`, `shadow_color`, `shadow_alpha`: drop shadow

    Animation knobs (`bounce_pixels`, `bounce_count`,
    `fade_in_seconds`) match image-mode intros for consistency. Use
    `update_intro` to tweak after seeing v1.

    What text mode CAN'T do: replicate the grunge/liquid/glitch
    textures of designed PNG logos. Use `create_intro` with a preset
    for those.
    """
    body: dict = {
        "name": name,
        "text": text,
        "font_path": font_path,
        "font_size": font_size,
        "font_color": font_color,
        "stroke_width": stroke_width,
        "stroke_color": stroke_color,
        "letter_spacing": letter_spacing,
        "alignment": alignment,
        "shadow_offset_x": shadow_offset_x,
        "shadow_offset_y": shadow_offset_y,
        "shadow_color": shadow_color,
        "shadow_alpha": shadow_alpha,
    }
    for k, v in (
        ("duration", duration),
        ("background_color", background_color),
        ("bounce_pixels", bounce_pixels),
        ("bounce_count", bounce_count),
        ("fade_in_seconds", fade_in_seconds),
        ("music_source_path", music_source_path),
        ("music_volume", music_volume),
    ):
        if v is not None:
            body[k] = v
    with _client() as c:
        return c.post(f"{API}/intros/text", json=body).json()


@mcp.tool()
def list_intro_presets() -> list[str]:
    """Names of preset logo PNGs in the intro library.

    Drop new `.png` files into `<workspace>/intros/_presets/` to add
    styles to your library. Then use `create_intro(preset=name)` to
    build an intro from one.
    """
    with _client() as c:
        return c.get(f"{API}/intros/presets").json()


@mcp.tool()
def list_intros() -> list[str]:
    """Names of existing intros under `WORKSPACE/intros/`. Each can be
    applied to any compilation via `set_compilation_intro`."""
    with _client() as c:
        return c.get(f"{API}/intros").json()


@mcp.tool()
def get_intro(name: str) -> dict:
    """Show current config + rendered mp4 path for one intro."""
    with _client() as c:
        return c.get(f"{API}/intros/{name}").json()


@mcp.tool()
def render_intro(name: str) -> dict:
    """Re-render an intro from its current `intro.json` config.

    Cheap — typically ~1 second. Use after hand-editing the JSON
    file. For most tuning, prefer `update_intro` which patches +
    re-renders in one shot.
    """
    with _client() as c:
        return c.post(f"{API}/intros/{name}/render").json()


@mcp.tool()
def update_intro(
    name: str,
    duration: float | None = None,
    background_color: str | None = None,
    logo_scale: float | None = None,
    bounce_pixels: int | None = None,
    bounce_count: int | None = None,
    fade_in_seconds: float | None = None,
    pulse_factor: float | None = None,
    music_volume: float | None = None,
    music_fade_out_seconds: float | None = None,
) -> dict:
    """Patch fields of an existing intro's config and re-render.

    Only fields you pass change — everything else is preserved.
    Common tweaks: `logo_scale` (0.55 default — bigger = word more
    prominent), `bounce_pixels` (40 default — more = livelier),
    `pulse_factor` (1.15 default — more = punchier impact).
    """
    body: dict = {}
    for k, v in (
        ("duration", duration),
        ("background_color", background_color),
        ("logo_scale", logo_scale),
        ("bounce_pixels", bounce_pixels),
        ("bounce_count", bounce_count),
        ("fade_in_seconds", fade_in_seconds),
        ("pulse_factor", pulse_factor),
        ("music_volume", music_volume),
        ("music_fade_out_seconds", music_fade_out_seconds),
    ):
        if v is not None:
            body[k] = v
    with _client() as c:
        return c.patch(f"{API}/intros/{name}", json=body).json()


@mcp.tool()
def set_compilation_intro(compilation_id: str, intro_name: str | None = None) -> dict:
    """Prepend a branded intro to the START of a compilation.

    `intro_name` is optional — omit it to use the workspace's
    default intro (set via `set_default_intro`).

    Replaces any existing intro on that reel (no double-stacking).
    The intro must already exist + be rendered (create one via
    `create_intro` first). Use `clear_compilation_intro` to remove.

    For inserting an intro between clips (not at the start) use
    `insert_compilation_intro_after` instead.
    """
    body: dict = {}
    if intro_name is not None:
        body["intro_name"] = intro_name
    return _compile_edit(compilation_id, "intro", body)


@mcp.tool()
def insert_compilation_intro_after(
    compilation_id: str,
    after_clip: str,
    intro_name: str | None = None,
) -> dict:
    """Insert a branded intro AFTER a specific clip in the compilation.

    Use this for chapter cards / transitions between gameplay clips:
    "add the intro after clip #3", "stick a stinger between the
    teamfights." Does NOT replace any existing intro at the start
    (unlike `set_compilation_intro`); inserts a new clip that pushes
    everything after it down by one position.

    `after_clip` accepts the same `clip_ref` forms as every other
    iterative-edit tool: 1-based index ("3"), UUID prefix, or
    "M:SS" time (reel time first, then source time).

    `intro_name` is optional — omit it to use the workspace's
    default intro (set via `set_default_intro`).
    """
    body: dict = {"after_clip": after_clip}
    if intro_name is not None:
        body["intro_name"] = intro_name
    with _client() as c:
        return c.post(
            f"{API}/edit/compile/{compilation_id}/insert_intro", json=body
        ).json()


@mcp.tool()
def insert_compilation_intro_at(
    compilation_id: str,
    position: int,
    intro_name: str | None = None,
) -> dict:
    """Insert a branded intro at a specific 1-based reel position.

    Position=1 prepends; position=N inserts before the existing clip
    at slot N. For "after clip X" semantics prefer
    `insert_compilation_intro_after` which is more natural.
    """
    body: dict = {"position": position}
    if intro_name is not None:
        body["intro_name"] = intro_name
    with _client() as c:
        return c.post(
            f"{API}/edit/compile/{compilation_id}/insert_intro", json=body
        ).json()


@mcp.tool()
def add_clip_caption(
    compilation_id: str,
    clip_ref: str,
    start_seconds: float,
    end_seconds: float,
    text: str,
    style_preset: str | None = None,
) -> dict:
    """Insert a single caption segment into a clip at a specific time.

    `start_seconds` and `end_seconds` are in CLIP source-time (same
    space as the clip's existing caption segments — match the format
    you see in the editor).

    `style_preset` is `"default"` (bottom-center, smaller) or
    `"tiktok"` (big top-center). Omit for default.

    Use this to add a missing line you noticed in playback, a
    branded callout like "PENTAKILL", or a chapter marker.
    """
    body: dict = {
        "clip_ref": clip_ref,
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
        "text": text,
    }
    if style_preset:
        body["style"] = {"preset": style_preset}
    with _client() as c:
        return c.post(f"{API}/edit/compile/{compilation_id}/clip_captions/add", json=body).json()


@mcp.tool()
def remove_clip_caption(compilation_id: str, clip_ref: str, segment_index: int) -> dict:
    """Delete one caption segment from a clip by its 0-based index.

    Use the index from `list_compilation_clips` (each clip returns
    `caption_segments` in order). Out-of-range indexes are silently
    ignored so stale indexes don't crash the edit.
    """
    body = {"clip_ref": clip_ref, "segment_index": segment_index}
    with _client() as c:
        return c.post(
            f"{API}/edit/compile/{compilation_id}/clip_captions/remove", json=body
        ).json()


@mcp.tool()
def tiktokify_clip_captions(compilation_id: str, clip_ref: str) -> dict:
    """Switch a clip's captions to TikTok style — explode + restyle.

    Replaces every caption segment in the clip with per-WORD segments
    tagged with the `tiktok` style preset. The result is the
    classic word-by-word karaoke effect. Uses Whisper word timings
    when available, falls back to even-split otherwise.

    Same effect as the legacy `tiktok_caption_clip` but uses the new
    unified data model. To undo, revert the journal to a prior
    version (the transformation is destructive — original segments
    aren't preserved).
    """
    with _client() as c:
        return c.post(
            f"{API}/edit/compile/{compilation_id}/clip_captions/tiktokify",
            json={"clip_ref": clip_ref},
        ).json()


@mcp.tool()
def edit_clip_captions(compilation_id: str, clip_ref: str, segments: list[dict]) -> dict:
    """Replace a clip's caption text with user-edited segments.

    Use this to fix mis-transcribed words (e.g. Whisper heard music as
    lyrics, missed a quiet line, mangled champion names) without
    re-running the whole STT pipeline.

    `segments` is the FULL updated list — not a patch. Each segment:
    ```
    {
      "start_seconds": 0.11,
      "end_seconds": 4.31,
      "text": "what am I doing man",
      "words": null  # optional; drop when you edited the text
    }
    ```

    When `words` is dropped on a changed segment, the renderer
    even-splits the new text across the segment's duration. Preserve
    `words` only for segments whose text matches the original Whisper
    output.

    The clip re-renders with new burnt-in captions. Whisper's master
    transcript in SQLite is NOT modified — this edit is local to the
    compilation's spec.json.
    """
    body = {"clip_ref": clip_ref, "segments": segments}
    with _client() as c:
        return c.post(
            f"{API}/edit/compile/{compilation_id}/clip_captions", json=body
        ).json()


@mcp.tool()
def reorder_compilation_clips(compilation_id: str, mode: str) -> dict:
    """Reorder the non-intro clips in a compilation by a scoring mode.

    INTRO clips stay where they are — they're chapter-card markers,
    not gameplay. Only the clips between/around them shuffle.

    Modes:
      - "chronological": story order, earliest source time first
      - "hype": highest-hype clip first, descending
      - "funny": highest-funny clip first
      - "story": highest-story clip first
      - "hook": top hype FIRST, rest chronological after

    Use to test different ordering strategies on the same reel
    without re-running the LLM ranker. Cached part files are
    reused for unchanged clips; only label re-encodes when
    `show_clip_numbers` is on.
    """
    with _client() as c:
        return c.post(
            f"{API}/edit/compile/{compilation_id}/reorder", json={"mode": mode}
        ).json()


@mcp.tool()
def reorder_compilation_clips_explicit(compilation_id: str, clip_ids: list[str]) -> dict:
    """Reorder clips by an EXPLICIT user-chosen sequence — drag-and-drop
    style.

    Unlike `reorder_compilation_clips` (which sorts by a scoring mode
    like "hype" or "chronological" and preserves intro positions), this
    tool takes the literal new full order of clip ids. Intros move
    freely with the rest.

    `clip_ids` must contain exactly the same id set as the current
    compilation — every existing id present, no extras, no duplicates.

    Use when the user has manually arranged the clips and you want to
    commit that arrangement (e.g. the drag-to-reorder filmstrip in the
    webapp uses this endpoint).
    """
    with _client() as c:
        return c.post(
            f"{API}/edit/compile/{compilation_id}/reorder_explicit",
            json={"clip_ids": clip_ids},
        ).json()


@mcp.tool()
def set_default_intro(intro_name: str | None) -> dict:
    """Mark an intro as the workspace default.

    Once set, `set_compilation_intro` and the insert-intro tools can
    be called without an `intro_name` argument and they'll use this
    default. Pass `intro_name=None` to clear the default.

    Useful when you have one go-to brand intro you want on every reel
    by default — set it once, then "add the intro" works everywhere.
    """
    with _client() as c:
        return c.post(f"{API}/intros/default", json={"intro_name": intro_name}).json()


@mcp.tool()
def get_default_intro() -> dict:
    """Return the workspace's current default intro name (or null)."""
    with _client() as c:
        return c.get(f"{API}/intros/default").json()


@mcp.tool()
def clear_compilation_intro(compilation_id: str) -> dict:
    """Remove the branded intro from a compilation if one is present.
    Returns the updated summary. No-op when no intro is set."""
    with _client() as c:
        return c.delete(f"{API}/edit/compile/{compilation_id}/intro").json()


@mcp.tool()
def list_compilation_history(compilation_id: str) -> dict:
    """Show the edit journal for a compilation — every spec change
    since the initial compile, oldest first.

    Each entry: `{version, ts, action, details, clip_count}`. Use the
    `version` to revert to a specific point via `revert_compilation`.

    Actions you might see:
    - `initial_compile` — the very first render (version 1)
    - `add_effect:zoom` / `add_effect:focus` / `add_effect:caption`
    - `extend_clip`, `insert_clip`, `remove_clip`
    - `caption_mode:tiktok` / `caption_mode:segment`
    - `labels:on` / `labels:off`
    - `set_intro`, `clear_intro`
    - `revert` (yes, reverts are journaled too — they're undoable)
    """
    with _client() as c:
        return c.get(f"{API}/edit/compile/{compilation_id}/history").json()


@mcp.tool()
def revert_compilation(
    compilation_id: str, steps: int = 1, to_version: int | None = None
) -> dict:
    """Undo edits by restoring an older spec snapshot from the journal.

    Two modes (use one):
    - `steps=N` — walk back N edits from the current state. `steps=1`
      (default) is the literal "undo last edit."
    - `to_version=N` — jump directly to a specific journal version
      (1-based, as listed by `list_compilation_history`).

    After restoring, the compilation re-renders so `compilation.mp4`
    matches. The revert itself is journaled so you can undo the undo.

    Errors if there isn't enough history (e.g. `steps=5` on a reel
    with only 3 edits).
    """
    body: dict = {"steps": steps}
    if to_version is not None:
        body["to_version"] = to_version
    with _client() as c:
        return c.post(f"{API}/edit/compile/{compilation_id}/revert", json=body).json()


@mcp.tool()
def regenerate_clip_thumbnails(compilation_id: str, force: bool = False) -> dict:
    """Generate per-clip thumbnail JPGs for the webapp filmstrip.

    Modern renders create these automatically; this tool backfills
    them for compilations made before the filmstrip feature shipped.
    `force=True` re-extracts even when files already exist.

    Returns `{total, ok, failed: [...]}`. Files land in
    `<compilation>/_thumbnails/<clip_id>.jpg`.
    """
    with _client() as c:
        return c.post(
            f"{API}/edit/compile/{compilation_id}/thumbnails/regenerate",
            params={"force": str(force).lower()},
        ).json()


@mcp.tool()
def regenerate_thumbnail(compilation_id: str) -> dict:
    """Re-extract the thumbnail JPG from the current `compilation.mp4`.

    Thumbnails auto-generate on every render. Call this only if you
    want a fresh thumbnail without re-rendering (rare). The chosen
    frame is the midpoint of the highest-`hype_score` non-intro clip
    — typically your hook moment.

    Returns `{ok, path, source_clip_id, source_clip_hype, seek_seconds}`.
    """
    with _client() as c:
        return c.post(f"{API}/edit/compile/{compilation_id}/thumbnail").json()


@mcp.tool()
def cleanup_compilation(compilation_id: str, dry_run: bool = False) -> dict:
    """Prune orphan cached part files in a compilation's `_parts/` folder.

    Iterative editing leaves stale cache: a removed clip's part file
    stays on disk, and inserting/removing clips shifts every later
    clip's `#NN` label index so old labeled-variant parts no longer
    match. This tool sweeps anything the current `spec.json` doesn't
    reference.

    **You usually don't need to call this** — `compile_highlights` and
    every iterative-edit tool (zoom/focus/caption/insert/remove/
    caption_mode/labels) auto-clean after a successful re-render. Use
    this for:
      - explicit sweeps after rapid batch edits via raw HTTP
      - `dry_run=True` to preview which files auto-cleanup would
        remove without deleting anything
      - recovery after a crashed render that left orphans behind

    Both label variants (no-label and `_n<current-index>`) are kept
    per clip so the instant label-flip cache survives. Returns
    `{deleted, deleted_files, freed_bytes, kept, dry_run, errors}`.
    Safe and idempotent.
    """
    with _client() as c:
        return c.post(
            f"{API}/edit/compile/{compilation_id}/cleanup",
            json={"dry_run": dry_run},
        ).json()


@mcp.tool()
def detect_champion(
    asset_id: str,
    at_seconds: float | None = None,
    min_confidence: float = 0.45,
) -> dict:
    """Identify which LoL champion the player is using via CV.

    Reads one mid-recording frame, crops the profile's
    `champion_portrait` region, and template-matches against Data
    Dragon's champion portrait set (downloaded once per version,
    cached locally). First call is slow (~30s while portraits
    download); later calls are quick.

    `at_seconds` overrides the auto mid-game sample point — useful
    if mid-game is covered by the death cam or another overlay.
    `min_confidence` is the NCC threshold below which we report
    no match rather than guessing.

    Returns the detection result: `{name, confidence, source: "cv",
    datadragon_version, sample_seconds}` — or `{name: null,
    reason: "no_match"}` when the best match fell below threshold.
    Job-based; this tool waits for completion.
    """
    body: dict = {"asset_id": asset_id, "min_confidence": min_confidence}
    if at_seconds is not None:
        body["at_seconds"] = at_seconds
    with _client() as c:
        job_id = c.post(f"{API}/league/detect_champion", json=body).json()["job_id"]
        job = _wait_for_job(c, job_id)
        if job.get("status") == "completed" and job.get("output_path"):
            # The job stores the structured result as JSON at output_path.
            # Fetch via the dedicated GET so the caller sees the actual
            # detection (not a file path).
            return c.get(f"{API}/league/champion/{asset_id}").json()
        return job


@mcp.tool()
def delete_vod_source(asset_id: str) -> dict:
    """Delete the source .mp4 of an INGESTED (downloaded via URL)
    asset to free disk space. The asset row + any compilations made
    from it stay intact — you just can't re-cut from the source after.

    Safety: this tool ONLY works on assets where source_origin =
    'downloaded' (files pulled via ingest_vod_url). Manually placed
    files (Outplayed recordings, hand-imported MP4s) are sacred and
    refuse to delete via this path.

    Use case: after a 1+ hour Twitch VOD has been compiled into a
    highlight reel, the 4GB source is dead weight. This frees that.

    Returns `{asset_id, freed_bytes, already_deleted}`. Idempotent.
    """
    with _client() as c:
        return c.post(f"{API}/assets/{asset_id}/delete_source").json()


@mcp.tool()
def split_vod_into_games(asset_id: str) -> dict:
    """Detect game boundaries in a long VOD and split it into per-game
    child files. Use this on Twitch scrim recordings that contain
    multiple games (2-4 typical) — each game becomes its own asset that
    can be analyzed + compiled independently.

    How it works:
    - ffmpeg `blackdetect` scans for dark transitions between games
      (loading screens, return-to-lobby fades, queue waits).
    - Stretches of black > 2 seconds are treated as boundaries.
    - Segments shorter than 60s are dropped as UI artifacts.
    - Each surviving segment gets a child file beside the parent:
      `scrim.mp4` -> `scrim_game1.mp4`, `scrim_game2.mp4`, ...
    - Child files use ffmpeg `-c copy` (no re-encode) — fast + lossless.

    Refuses to run if the source file was deleted via
    `delete_vod_source` (re-import first). Returns "no split needed"
    when only one segment was detected (the VOD looks single-game).

    Job-based; this tool polls to completion. On success, `output_path`
    contains a comma-separated list of new child asset ids.
    """
    with _client() as c:
        job_id = c.post(f"{API}/assets/{asset_id}/split").json()["job_id"]
        return _wait_for_job(c, job_id)


@mcp.tool()
def regenerate_asset_thumbnail(asset_id: str) -> dict:
    """Re-extract a source asset's poster frame.

    Source asset thumbnails are auto-extracted during `scan_assets`,
    but assets indexed before that feature shipped don't have one.
    Call this to backfill — or to refresh after deleting the cached
    file.

    Output lives at `<workspace>/asset_thumbnails/<asset_id>.jpg` and
    is served by the StaticFiles mount at `/workspace/asset_thumbnails/...`.
    Idempotent: re-running just overwrites the existing JPG.
    """
    with _client() as c:
        return c.post(f"{API}/assets/{asset_id}/thumbnail").json()


@mcp.tool()
def backfill_asset_durations() -> dict:
    """Backfill cached ffprobe durations for assets indexed before the
    duration column shipped.

    The gallery uses `duration_seconds` to gate features (e.g. the
    "split into games" button only shows on recordings longer than
    1 hour). Existing rows from before this column shipped have NULL
    duration and the gallery hides the button on them.

    Safe to call repeatedly: only NULL rows are probed. Returns the
    count of rows queued (work happens in a background task, so
    durations populate over the next minute or so).
    """
    with _client() as c:
        return c.post(f"{API}/assets/backfill_durations").json()


@mcp.tool()
def vlm_health() -> dict:
    """Report whether the VLM (vision language model) taste-layer backend
    is reachable + which model is active + a canary latency reading.

    The VLM is used to validate each cut clip and review the whole
    compilation as it's being built. It's an OPTIONAL enrichment step —
    when unavailable, compiles run without validation.

    Returns a dict like `{ok, backend, enabled, model, latency_ms,
    reason}`. If `ok` is False, `reason` explains why (Ollama not
    installed, model not pulled, VLM_ENABLED=false, etc). Zero side
    effects; safe to call repeatedly.
    """
    with _client() as c:
        return c.post(f"{API}/vlm/health").json()


@mcp.tool()
def compile_shorts(
    asset_id: str,
    mode: str,
    topic: str | None = None,
    music_path: str | None = None,
) -> dict:
    """Render YouTube Shorts / TikTok / Reels-ready 9:16 videos from a
    game's highlights folder. Requires the asset to already have been
    analyzed with `cut=True` (so the highlights folder + clip files
    exist on disk).

    Two modes:

    - `voiceover` — 1 clip per short, source audio ducked to 20% so
      you can dub your own narration on top in your DAW. Best for
      "how I set up this play"-style commentary shorts. The generated
      `index.md` includes a suggested voice-over prompt per short
      (e.g. "How I set up this kill in laning phase").
    - `montage` — 2-N adjacent clips packed into one short (chronological
      order). Optional royalty-free music bed via `music_path` param
      or the `SHORTS_DEFAULT_MUSIC_PATH` config setting.

    `topic` filters which narrative buckets get compiled. Available
    buckets: `first_blood`, `laning_phase`, `mid_game`, `late_game`,
    `objective_steal`, `multikill`, `teamfight`, `outplay`. Substring
    match, case-insensitive — `topic="kill"` matches `multikill`. Omit
    to compile all non-empty buckets. Preview available buckets with
    `list_shorts_topics` before committing.

    Deterministic: same asset + same args -> same output files. Every
    short passes through the existing VLM coherence check; results
    land in `index.md`.

    Output folder: `WORKSPACE/shorts/<asset-stem>_<mode>/`. Filenames
    are `short_<NN>_<bucket>_<mmss>.mp4` sorted chronologically by
    each short's earliest clip.

    Returns the job result — poll internally handled; the tool waits
    for completion. On timeout the job continues in the background.
    """
    if mode not in ("voiceover", "montage"):
        return {"status": "failed", "error": "mode must be 'voiceover' or 'montage'"}
    body: dict = {"mode": mode}
    if topic:
        body["topic"] = topic
    if music_path:
        body["music_path"] = music_path
    with _client() as c:
        job_id = c.post(f"{API}/assets/{asset_id}/shorts", json=body).json()["job_id"]
        return _wait_for_job(c, job_id)


@mcp.tool()
def list_shorts(asset_id: str, mode: str | None = None) -> dict:
    """List rendered shorts on disk for an asset.

    Read-only — does not trigger a render. `mode` filters to
    `voiceover` or `montage`; omit for both. Returns the folder path,
    the list of short filenames, and the full `index.md` contents
    (which include per-short bucket, title overlay, VO prompt, music
    used, and VLM coherence verdict).
    """
    params = f"?mode={mode}" if mode in ("voiceover", "montage") else ""
    with _client() as c:
        return c.get(f"{API}/assets/{asset_id}/shorts{params}").json()


@mcp.tool()
def list_shorts_topics(asset_id: str) -> dict:
    """Preview which narrative buckets are available for this asset
    without rendering anything.

    Reads the highlights folder + `index.json`, runs the bucketing
    rules (phase-based + event-based + adjacency-based), and returns
    a per-bucket clip count with a small clip preview each. Requires
    the asset to have been analyzed with `cut=True` first.

    Use this before `compile_shorts` to know:
    - Which buckets have enough clips to compile
    - Which clips fall below the hype threshold (dropped)
    - Whether you want to filter with `topic=<bucket>` or compile all
    """
    with _client() as c:
        return c.get(f"{API}/assets/{asset_id}/shorts/topics").json()


@mcp.tool()
def vlm_review_compilation(
    compilation_id: str,
    max_passes: int | None = None,
    n_frames: int | None = None,
) -> dict:
    """Run the VLM taste-layer review on a rendered compilation.

    Samples ~30-60 frames spread across the compiled reel and returns
    a list of suggested fixes to improve pacing / cohesion / variety.
    Each fix names a clip (by `clip_ref` — 1-based index, UUID prefix,
    or M:SS timestamp) and one of: extend_before, extend_after,
    trim_start, trim_end, remove_clip, apply_zoom, apply_focus.

    This tool is REVIEW-ONLY — no mutations are applied. Use the
    returned `fixes` list as guidance for follow-up editing tool calls
    (`extend_compilation_clip`, `remove_compilation_clip`,
    `zoom_compilation_clip`, `focus_compilation_clip`). The user (or a
    higher-level agent) decides which fixes to actually apply.

    Requires the compilation to already be rendered (call `finalize_
    compilation` first). Requires VLM_ENABLED=true + a reachable
    backend — check `vlm_health` first if the returned fixes list is
    empty and something feels off.

    Returns `{ok, passes, is_cohesive, fixes[], backend, model}`.
    `is_cohesive: true` with empty fixes means the reel looks good.
    """
    body: dict = {}
    if max_passes is not None:
        body["max_passes"] = max_passes
    if n_frames is not None:
        body["n_frames"] = n_frames
    with _client() as c:
        return c.post(
            f"{API}/edit/compile/{compilation_id}/vlm_review",
            json=body,
        ).json()


@mcp.tool()
def ingest_vod_url(url: str, game: str) -> dict:
    """Download a VOD from a URL (Twitch / YouTube / etc) into the
    local Outplayed media folder, ready to be analyzed + compiled like
    any other recording.

    `url` must be HTTPS. `game` is the subfolder (e.g. 'league' or
    'valorant'); it must match an existing or new directory under
    OUTPLAYED_MEDIA_DIR. Lowercase alphanumeric only (no spaces).

    Uses yt-dlp under the hood — must be installed on PATH
    (`pip install yt-dlp`). Source video stays local; nothing is
    uploaded. The new asset is tagged `source_origin='downloaded'` so
    the cleanup tool can auto-delete it after compile (manual scans
    are tagged 'imported' and never auto-deleted).

    Returns the new asset's job result + asset_id (`output_path` field
    of the job carries the asset id on success). Job-based; this tool
    waits for completion. Long downloads may report 'timeout' here
    while still running — re-check with `get_job`.
    """
    with _client() as c:
        job_id = c.post(
            f"{API}/assets/ingest_url",
            json={"url": url, "game": game},
        ).json()["job_id"]
        job = _wait_for_job(c, job_id)
        return job


@mcp.tool()
def get_feedback_summary(compilation_id: str | None = None) -> dict:
    """Show how the user's manual edits compare to the system's defaults.

    Every time the user extends, removes, or reverts a clip in the webapp
    (or via MCP edit tools), the action is logged. This summarises:

    - `total_events` — how many user-edit actions have been logged
    - `by_action` — count per action type (extend / remove_clip / revert)
    - `by_event_type` — count per clip event_type (funny_audio, kill, etc.)
    - `extend_medians_per_event_type` — for each event_type, the median
      seconds the user added BEFORE/AFTER. Tells you "the system tends
      to clip funny moments 3s too short on average."
    - `proposed_event_window_overrides` — the median values the system
      WOULD set as new `event_window_overrides` defaults IF you trusted
      the signal directly. Currently advisory only; the system does NOT
      auto-apply these. Review them, then update `.env` manually.

    Pass `compilation_id` to scope stats to one reel; omit for all-time.
    """
    params: dict = {}
    if compilation_id is not None:
        params["compilation_id"] = compilation_id
    with _client() as c:
        return c.get(f"{API}/feedback/summary", params=params).json()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
