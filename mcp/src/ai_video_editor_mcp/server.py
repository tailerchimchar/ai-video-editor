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


def _wait_for_job(client: httpx.Client, job_id: str, timeout_s: float = _JOB_TIMEOUT_S) -> dict:
    """Poll a background job until it finishes (or times out)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = client.get(f"{API}/jobs/{job_id}").json()
        if job.get("status") in ("completed", "failed"):
            return job
        time.sleep(2)
    return {"status": "timeout", "error": f"job {job_id} did not finish in {timeout_s:g}s"}


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
    """Transcribe one recording with the configured Whisper backend.
    Local by default (private, $0, via faster-whisper) — set
    `TRANSCRIBE_BACKEND=groq` in the API's .env for cloud transcription
    (~$0.02/full VOD via whisper-large-v3-turbo, ~30s wall-clock).

    Waits for the job and returns the segment count + a short preview.
    Short clips finish in seconds; long VODs on local CPU are slow
    and may report 'timeout' here while still running — re-check with
    get_transcript / get_job. Stored once, then reused by candidate
    generation (transcript_keyword source scans hype/funny cues and
    VADER high-arousal segments)."""
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
def list_session_buckets(game: str, session: str) -> dict:
    """Route an analyzed session's clips into the four mentor buckets:
    `hype`, `story`, `voice_over`, `funny`.

    Reads the session's `highlights/<game>/<session>/index.json` and
    applies pure rules:

    - `hype` -- aces / 4Ks / 3Ks only. No 1Ks or 2Ks (mentor rule).
    - `funny` -- funny_callout clips or funny_score >= 0.8 kills.
    - `voice_over` -- single kills, 2Ks, hype callouts, deaths (VO
      storytelling material).
    - `story` -- reserved for manual picks; round-arc auto-detection
      is not yet built.

    Args:
      - `game` -- e.g. "Valorant", "League of Legends"
      - `session` -- session folder name, e.g. "07-07-2026_23h44"

    Returns a manifest with per-bucket clip lists (file, event_type,
    hype_score, funny_score, story_score, reason). Cheap -- one JSON
    read, no LLM, no ffmpeg. Use this BEFORE deciding what to compile
    so you route the right clips to the right compile call:

    - hype list -> compile_custom_montage with hype pool
    - funny list -> compile_custom_montage tagged for funny editing
    - voice_over list -> hold for VO scripting
    """
    with _client() as c:
        return c.get(
            f"{API}/highlights/{game}/{session}/buckets"
        ).json()


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
    'scoreline_lol', 'minimap_lol', or 'full'. `aspect` is '16:9',
    '9:16' (center-crop vertical), or '9:16_hud' (HUD-preserving
    vertical; requires HUD_ASPECT_ENABLED on the API). Renders a new
    .mp4 to WORKSPACE/edits/."""
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
    auto path. `aspect` is '16:9', '9:16', or '9:16_hud' (HUD layout;
    requires HUD_ASPECT_ENABLED — defaults caption y to the top band)."""
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
    `radius * min(w, h)`. `dim` is the outside-circle brightness (0..1).
    `aspect` is '16:9', '9:16', or '9:16_hud' (requires HUD_ASPECT_ENABLED)."""
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

    Safety: this tool ONLY works on assets where source_origin is
    'downloaded' (files pulled via ingest_vod_url) or 'cloud' (pulled via
    download_vod_from_cloud). Manually placed files (Outplayed recordings,
    hand-imported MP4s) are sacred and refuse to delete via this path.
    A 'cloud' file is only deleted while its cloud copy is intact (same
    size), so it can always be pulled again; otherwise it refuses (409).

    Cleanup after a cloud pull: once the user is done rendering a pulled
    game, call this on its asset_id to free the disk space.

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
def upload_vod_to_cloud(asset_id: str) -> dict:
    """Back up ONE raw recording to the cloud (Supabase Storage, private
    `outplayed` bucket). Nothing is ever uploaded automatically — use this
    when the user says a game is worth keeping ("upload the Jhin game",
    "save that VOD to the cloud"). Find the asset id with `list_assets`.

    The file keeps its folder layout: `League of Legends/<session>/<file>.mp4`.
    The local file is only read, never changed or deleted. Re-uploading an
    unchanged VOD is a no-op ("already uploaded").

    Refuses if the upload would push total cloud storage past the budget
    (100 GB by default) — suggest `remove_vod_from_cloud` on a weaker game.

    VODs are multi-GB, so this waits up to 3 minutes. If the result is
    `status: "timeout"` the upload is STILL RUNNING in the background — tell
    the user, and check later with `get_job(job_id)`; do not re-submit.
    On success `output_path` is `outplayed:<key>`.
    """
    with _client() as c:
        job_id = c.post(f"{API}/assets/{asset_id}/cloud").json()["job_id"]
        job = _wait_for_job(c, job_id, timeout_s=180)
        job.setdefault("job_id", job_id)
        return job


@mcp.tool()
def list_cloud_vods() -> dict:
    """List the raw VODs backed up in the cloud, with size, upload time and
    the matching local `asset_id` (null if that recording isn't indexed
    here). Also returns `used_bytes` (all cloud buckets) and `budget_gb`,
    so you can tell the user how much room is left."""
    with _client() as c:
        return c.get(f"{API}/cloud/vods").json()


@mcp.tool()
def download_vod_from_cloud(key: str) -> dict:
    """Pull a raw VOD from the cloud onto THIS machine so the normal
    pipeline (candidates, analyze, story/montage render) can run on it.
    Use when the user says "pull the Jhin game" / "grab that VOD from the
    cloud". Get `key` from `list_cloud_vods` — entries with `asset_id: null`
    are in the cloud but not on this machine.

    The file lands at the same relative path under the local Outplayed
    folder and is indexed automatically (no separate scan needed). A VOD
    that's already here at the same size is not downloaded again.

    Processing straight from the cloud isn't supported: the pipeline reads
    the whole file several times, so download once, work, then clean up.

    Waits up to 3 minutes; on `status: "timeout"` the download is STILL
    RUNNING — check later with `get_job(job_id)`, don't re-submit. On
    success `output_path` is the asset id.

    CLEANUP: when the user is done with this game, call
    `delete_vod_source(asset_id)` to delete the local copy (the cloud copy
    stays). Remind the user if they move on without cleaning up.
    """
    with _client() as c:
        job_id = c.post(f"{API}/cloud/vods/pull", json={"key": key}).json()["job_id"]
        job = _wait_for_job(c, job_id, timeout_s=180)
        job.setdefault("job_id", job_id)
        return job


@mcp.tool()
def remove_vod_from_cloud(asset_id: str) -> dict:
    """Delete a recording's CLOUD copy only (e.g. it was uploaded by mistake
    or to make room under the budget). The local file on disk is NOT
    touched. This cannot be undone for the cloud copy — confirm with the
    user which game it is before calling. Returns `{removed: false}` if it
    wasn't in the cloud."""
    with _client() as c:
        return c.delete(f"{API}/assets/{asset_id}/cloud").json()


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
    layout: str | None = None,
    beat_flash: bool | None = None,
    beat_flash_source: str | None = None,
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
    are `short_<NN>_[HIGH_]<bucket>_<mmss>.mp4`.

    **Voice-over mode is priority-ranked** — `short_01` is the user's
    top-ranked category, not the earliest in-game moment. Ranking
    (highest to lowest): laning-phase kills > 3+-kill multikills >
    teamfights that "turn a game around" > everything else. The
    `HIGH_` filename infix flags 3+-kill multikills. Montage mode
    stays chronological (adjacency-cluster order preserved).

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
    if layout:
        body["layout"] = layout
    if beat_flash is not None:
        body["beat_flash"] = beat_flash
    if beat_flash_source:
        body["beat_flash_source"] = beat_flash_source
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
def download_music(url: str, name: str | None = None) -> dict:
    """Download a music track from a URL (YouTube supported) into
    `WORKSPACE/music/` and return its local path.

    Use the returned `path` as the `music_path` argument to
    `compile_shorts` or `compile_cross_asset_montage` for a montage
    with a music bed. Optionally enable `beat_flash=True` on those
    compile calls to overlay a subtle pulse on each beat.

    Idempotent: repeat calls with the same URL return the cached file
    (hashed by URL). `name` optionally overrides the filename stem
    (e.g. `name="beat_drop"` -> `beat_drop.mp3`).

    Local paths (already a file) are returned verbatim — safe to pass
    an already-downloaded file's path.
    """
    body: dict = {"url": url}
    if name:
        body["name"] = name
    with _client() as c:
        return c.post(f"{API}/shorts/music", json=body).json()


@mcp.tool()
def compile_cross_asset_montage(
    game: str,
    window: str = "week",
    music_path: str | None = None,
    layout: str | None = None,
    beat_flash: bool | None = None,
    beat_flash_source: str | None = None,
) -> dict:
    """Compile ONE "best of" montage short from your top multikill
    plays across EVERY session for `game` — not just one recording.

    Sweeps `WORKSPACE/highlights/<game>/<session>/` folders inside
    the time window, keeps only the multikill clusters (2+ adjacent
    kills within 30 seconds), and ranks them across sessions:
    kill-group size DESC (triples/quadras beat doubles), then hype
    score DESC, then most-recent session first. Renders one 9:16
    montage short using the same layout / audio / VLM check as
    single-asset `compile_shorts`.

    `window` options:
    - `day`  — today and yesterday only
    - `week` — last 7 days (great for a weekly "best of" post)
    - `all`  — every session on disk (use when the week window is
      empty; typical for testing on old recordings)

    Output: `WORKSPACE/shorts/_cross_asset/<game>_<window>_<YYYY-MM-DD>/
    best_of_[HIGH_]<window>_<date>.mp4` + an `index.md` that lists
    every session scanned and every clip picked (with source folder,
    kill-group size, and hype score). The `HIGH_` infix appears when
    any 3+-kill cluster is included.

    Deterministic: same folder state + same args → identical output.
    Requires each source session's `analyze_asset(cut=True)` to have
    already run (produces the highlights folder + clip files).

    For a music bed, first call `download_music(url)` to get a local
    path, then pass it as `music_path`. Enable `beat_flash=True` to
    overlay a subtle white pulse on each beat — makes the montage
    feel much more hype for basically free.

    Poll internally — the tool waits for completion (typically
    2-5 minutes depending on clip count).
    """
    if window not in ("day", "week", "all"):
        return {"status": "failed", "error": "window must be 'day', 'week', or 'all'"}
    body: dict = {"game": game, "window": window}
    if music_path:
        body["music_path"] = music_path
    if layout:
        body["layout"] = layout
    if beat_flash is not None:
        body["beat_flash"] = beat_flash
    if beat_flash_source:
        body["beat_flash_source"] = beat_flash_source
    with _client() as c:
        job_id = c.post(f"{API}/shorts/cross_asset_montage", json=body).json()[
            "job_id"
        ]
        return _wait_for_job(c, job_id)


@mcp.tool()
def preview_cross_asset_montage(game: str, window: str = "week") -> dict:
    """Dry-run: preview WHICH clips would be compiled by
    `compile_cross_asset_montage` — without rendering.

    Reads every session folder for `game` inside the window, ranks the
    multikill clips, and returns the top candidates with their source
    session, kill-group size, hype score, and reason. Cheap — walks
    JSON files only, no ffmpeg.

    Use this before committing to a 5-minute compile to sanity-check
    the pick.
    """
    if window not in ("day", "week", "all"):
        return {"status": "failed", "error": "window must be 'day', 'week', or 'all'"}
    with _client() as c:
        return c.get(
            f"{API}/shorts/cross_asset_montage/preview?game={game}&window={window}"
        ).json()


@mcp.tool()
def list_cross_asset_shorts(game: str | None = None) -> dict:
    """Enumerate cross-asset "best of" shorts already rendered on disk
    under `WORKSPACE/shorts/_cross_asset/`.

    Read-only. Filter by `game` (substring match on folder prefix) to
    narrow down. Returns one entry per rendered folder with the folder
    path, its list of `.mp4` filenames, and whether `index.md` is
    present.
    """
    params = f"?game={game}" if game else ""
    with _client() as c:
        return c.get(f"{API}/shorts/cross_asset_montage{params}").json()


@mcp.tool()
def compile_outplayed_montage(
    game: str,
    limit: int | None = None,
    since_days: int | None = None,
    music_path: str | None = None,
    layout: str | None = None,
    beat_flash: bool | None = None,
    beat_flash_source: str | None = None,
    use_clip_analyzer: bool | None = None,
    analyze_pool_size: int | None = None,
) -> dict:
    """Compile ONE montage DIRECTLY from Outplayed's auto-cut clips
    for `game` — no analysis or LLM ranking needed. Outplayed already
    picked the good moments (each clip is a detected event), so we
    just stitch the newest N together.

    Best for:
    - Valorant (no Riot API, but Outplayed detects kills/aces cleanly)
    - Any game where you have a fresh session and want a quick
      "here's what I hit tonight" montage

    Args:
      - `limit`: max clips (default 5)
      - `since_days`: restrict to clips from the last N days. `1` = today
        only. Omit for all-time.
      - `music_path`: local music file (call `download_music(url)` first)
      - `beat_flash`: True to overlay pulses; `beat_flash_source="kills"`
        (default) fires on each clip's payoff moment
      - `use_clip_analyzer`: When true (default from config), a per-clip
        VLM analyzer watches each source clip and decides the trim
        window — cutting Outplayed's padded pre-roll and hunting for
        the actual payoff moment. Uses the editing rubric under
        vlm/game_hints/<game>.md. Pass False to skip and use whole
        clips (faster but less tight).

    Output: `WORKSPACE/shorts/_outplayed/<game>_<YYYY-MM-DD>/
    best_of_<YYYY-MM-DD>.mp4` + an `index.md` listing every clip picked.

    Deterministic: same clip set on disk + same args → same output
    (analyzer results are cached in a sidecar next to each source clip).
    Poll internally; typically 30s-3min to render 5 clips (first run
    with analyzer takes longer; re-runs hit the cache and are fast).
    """
    body: dict = {"game": game}
    if limit is not None:
        body["limit"] = limit
    if since_days is not None:
        body["since_days"] = since_days
    if music_path:
        body["music_path"] = music_path
    if layout:
        body["layout"] = layout
    if beat_flash is not None:
        body["beat_flash"] = beat_flash
    if beat_flash_source:
        body["beat_flash_source"] = beat_flash_source
    if use_clip_analyzer is not None:
        body["use_clip_analyzer"] = use_clip_analyzer
    if analyze_pool_size is not None:
        body["analyze_pool_size"] = analyze_pool_size
    with _client() as c:
        job_id = c.post(f"{API}/shorts/outplayed_montage", json=body).json()[
            "job_id"
        ]
        return _wait_for_job(c, job_id)


@mcp.tool()
def preview_outplayed_montage(
    game: str,
    limit: int | None = None,
    since_days: int | None = None,
) -> dict:
    """Dry-run: preview which Outplayed clips would be picked by
    `compile_outplayed_montage`, without rendering.

    Cheap (ffprobe only, no ffmpeg). Use to see which clips are
    freshest before committing to a compile.
    """
    params = [f"game={game}"]
    if limit is not None:
        params.append(f"limit={limit}")
    if since_days is not None:
        params.append(f"since_days={since_days}")
    with _client() as c:
        return c.get(
            f"{API}/shorts/outplayed_montage/preview?{'&'.join(params)}"
        ).json()


@mcp.tool()
def list_outplayed_shorts(game: str | None = None) -> dict:
    """List Outplayed direct-compile shorts already rendered under
    `WORKSPACE/shorts/_outplayed/`. Read-only. Optionally filter by
    game (substring on folder prefix).
    """
    params = f"?game={game}" if game else ""
    with _client() as c:
        return c.get(f"{API}/shorts/outplayed_montage{params}").json()


# ---------------------------------------------------------------------
# Browse both source locations (Outplayed folder + workspace highlights)
# — so you can hand-pick clip paths before calling compile_custom_montage.
# ---------------------------------------------------------------------


@mcp.tool()
def list_outplayed_clips(
    game: str,
    since_days: int | None = None,
    limit: int | None = None,
) -> dict:
    """Browse Outplayed's auto-cut event clips for `game`.

    Walks `OUTPLAYED_MEDIA_DIR/<game>/`, returns each short clip
    (<=120s) with its path, source session folder, duration, and
    modification time. Most recent first.

    Use to see what's available before calling
    `compile_custom_montage(clip_paths=[...])` with hand-picked paths.
    Or pass `since_days=1` to see just today's clips.
    """
    params = [f"game={game}"]
    if since_days is not None:
        params.append(f"since_days={since_days}")
    if limit is not None:
        params.append(f"limit={limit}")
    with _client() as c:
        return c.get(
            f"{API}/shorts/browse/outplayed?{'&'.join(params)}"
        ).json()


@mcp.tool()
def list_highlights_sessions(game: str | None = None) -> dict:
    """Browse analyzed session folders under
    `WORKSPACE/highlights/<game>/`. Each folder is a game session
    that went through `analyze_asset(cut=True)` — its clips are
    already ranked + cut into short mp4s.

    Filter by `game` (exact name); omit for every game in the
    workspace. Use with `list_highlights_clips(session_folder)` to
    see what's inside a specific session.
    """
    params = f"?game={game}" if game else ""
    with _client() as c:
        return c.get(f"{API}/shorts/browse/highlights{params}").json()


@mcp.tool()
def list_highlights_clips(
    session_folder: str, game: str | None = None
) -> dict:
    """List clips inside one analyzed highlights session folder.

    `session_folder` is the folder name (e.g. `07-04-2026_15h15`,
    from `list_highlights_sessions`). Returns each clip's path,
    event type, timing, hype score, and the ranker's reason string.

    Use these paths as inputs to `compile_custom_montage`.
    """
    params = f"?game={game}" if game else ""
    with _client() as c:
        return c.get(
            f"{API}/shorts/browse/highlights/{session_folder}{params}"
        ).json()


# ---------------------------------------------------------------------
# Custom montage — stitch a hand-picked list of paths from anywhere.
# ---------------------------------------------------------------------


@mcp.tool()
def compile_custom_montage(
    clip_paths: list[str],
    name: str | None = None,
    game: str | None = None,
    music_path: str | None = None,
    layout: str | None = None,
    beat_flash: bool | None = None,
    beat_flash_source: str | None = None,
    use_clip_analyzer: bool | None = None,
    max_seconds: float | None = None,
    burn_title: bool | None = None,
) -> dict:
    """Compile ONE montage from an EXPLICIT list of clip paths — mix
    from ANYWHERE: Outplayed folder, analyzed highlights folders,
    ad-hoc directories. Order in the list = order in the output.

    `max_seconds` overrides the config-level duration cap for THIS
    compile only. Default (config) is 180s -- long enough for a full
    Valorant round. Pass e.g. 60 for a hype short cap or 300 for a
    Spike Rush match. Omit to use the config default.

    `burn_title` controls whether the title overlay is drawn. When
    MONTAGE_TITLE_OPTIONAL is off (default), this parameter is ignored
    and titles always appear. When the flag is on, pass False to skip
    the title overlay.

    This is the "hey, take these specific clips and put them together"
    flow — complements (does not replace) the automated
    `compile_shorts` / `compile_outplayed_montage` /
    `compile_cross_asset_montage` flows.

    Typical workflow:
      1. Browse: `list_outplayed_clips("Valorant")` or
         `list_highlights_sessions("League of Legends")` +
         `list_highlights_clips(session)` to see what's available
      2. Pick clip paths you want
      3. `compile_custom_montage(clip_paths=[...], music_path="...",
         beat_flash=True)` — renders one 9:16 montage.

    Args:
      - `clip_paths`: list of file paths (any existing `.mp4` works)
      - `name`: output folder + filename stem. Omit for timestamp.
      - `music_path`: pass a local path (call `download_music(url)`
        first to fetch from YouTube)
      - `beat_flash` + `beat_flash_source`: same as other compile
        tools — `kills` default, `downbeats` / `beats` also available
      - `burn_title`: True (default) burns the title overlay. When
        MONTAGE_TITLE_OPTIONAL is on, pass False to skip the overlay.

    Output: `WORKSPACE/shorts/_custom/<name>/<name>.mp4` + `index.md`.
    Missing / unreadable paths are skipped with a warning; the montage
    still renders with whatever survived.
    """
    if not clip_paths:
        return {"status": "failed", "error": "clip_paths cannot be empty"}
    body: dict = {"clip_paths": clip_paths}
    if name:
        body["name"] = name
    if game:
        body["game"] = game
    if music_path:
        body["music_path"] = music_path
    if layout:
        body["layout"] = layout
    if beat_flash is not None:
        body["beat_flash"] = beat_flash
    if beat_flash_source:
        body["beat_flash_source"] = beat_flash_source
    if use_clip_analyzer is not None:
        body["use_clip_analyzer"] = use_clip_analyzer
    if max_seconds is not None:
        body["max_seconds"] = max_seconds
    if burn_title is not None:
        body["burn_title"] = burn_title
    with _client() as c:
        job_id = c.post(f"{API}/shorts/custom_montage", json=body).json()[
            "job_id"
        ]
        return _wait_for_job(c, job_id)


@mcp.tool()
def list_custom_shorts() -> dict:
    """List custom-compile montages already rendered under
    `WORKSPACE/shorts/_custom/`. Read-only.
    """
    with _client() as c:
        return c.get(f"{API}/shorts/custom_montage").json()


@mcp.tool()
def trim_source_to_clip(
    source_path: str,
    start_seconds: float,
    end_seconds: float,
    name: str | None = None,
    anchor_to_kill: bool = False,
) -> dict:
    """Cut a window out of any .mp4 into a stable file the editing
    pipeline can consume. Sync -- returns the output path immediately.

    The building block for JUMP-CUT edits and story shorts. Chain 2-4
    calls to slice one long source into short sub-clips, then feed the
    output paths to `compile_custom_montage(clip_paths=[...])` to get
    a jump-cut compilation.

    Example -- turning a 40s clip into a 3-cut comedy short:

        a = trim_source_to_clip(src, 25, 35, "setup")
        b = trim_source_to_clip(src, 75, 95, "climax")
        c = trim_source_to_clip(src, 91, 100, "punchline")
        compile_custom_montage(clip_paths=[a["output_path"], b["output_path"], c["output_path"]])

    Args:
      - `source_path`: absolute path to any .mp4 on disk.
      - `start_seconds`, `end_seconds`: cut window.
      - `name`: output filename stem (no extension). Omit to derive
        from source name + range.
      - `anchor_to_kill`: when True, snap the window to the nearest
        ground-truth Overwolf kill anchor within 5s of the requested
        midpoint. Uses per-payoff-type pre/post buffers (single kill:
        3s/2.5s, ace: 6s/4s). Falls through unchanged when the source
        isn't in Outplayed's dispatch log or no anchor is close. The
        response includes `snapped`, `anchor_seconds`, `event_type`.

    Output: `WORKSPACE/trims/<name>.mp4`. Stream-copy (no re-encode) so
    it finishes in <1s regardless of clip length. Same source file
    stays immutable.
    """
    body = {
        "source_path": source_path,
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
        "anchor_to_kill": anchor_to_kill,
    }
    if name:
        body["name"] = name
    with _client() as c:
        r = c.post(f"{API}/clips/trim_source", json=body)
        r.raise_for_status()
        return r.json()


@mcp.tool()
def list_kill_anchors(
    source_path: str,
    include_clusters: bool = True,
) -> dict:
    """Preview ground-truth kill anchors from Outplayed's Overwolf log
    for a source .mp4. Empty when the file isn't in the dispatch table
    (log rotates ~2 weeks; user-clipped shorts skip the log; non-Val
    games don't have this data).

    Use this BEFORE `auto_cut_kills_from_source` to see what the log
    contains -- lets you decide whether to trust the auto-cut or fall
    back to hand-picked windows.

    Args:
      - `include_clusters`: include multi-kill anchors (double / triple
        / quad / ace) alongside individual kills. Set False for
        voice-over pacing where each frag should land separately.

    Returns:
        {
          "count": int,
          "anchors": [
            {"anchor_seconds": float, "event_type": str,
             "cluster_size": int|null, "headshot": bool}
          ]
        }
    """
    body = {"source_path": source_path, "include_clusters": include_clusters}
    with _client() as c:
        r = c.post(f"{API}/clips/list_kill_anchors", json=body)
        r.raise_for_status()
        return r.json()


@mcp.tool()
def mentor_record_correction(
    game: str,
    feedback: str,
    rule: str,
    clip_ref: str | None = None,
    source_context: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Record a structured editorial correction so the NEXT compile
    inherits it. Persisted to `vlm/game_hints/<game>_examples.jsonl`;
    auto-loaded into every VLM system prompt for that game; its content
    hash rolls into the effective PROMPT_VERSION so per-clip analyzer
    caches invalidate cleanly.

    This is the DSPy-style memory flywheel of the anchor+critic
    pipeline. When the user says "kills should be off by less than 1s"
    or "stop using scoreboard shots as endings", call this immediately
    with the RULE, not just the feedback. Future compiles pattern-match
    on it.

    Args:
      - `game`: 'valorant', 'league', etc. Case-insensitive.
      - `feedback`: verbatim short quote of what the user said.
      - `rule`: the actionable rule the LLM should apply next time.
        Be specific: not "be better", but "sub-clip must include the
        killfeed pop within 1s of the frag".
      - `clip_ref`: optional -- filename or short-name of the clip
        that triggered this correction.
      - `tags`: freeform, for future filtering (e.g. ["anchor",
        "may30", "punchline_timing"]).

    Example -- capturing the "kills off by 1s" issue from THIS session:

        mentor_record_correction(
            game="valorant",
            feedback="kills were off by 1s",
            rule="every sub-clip must include the killfeed pop; extend "
                 "the end by 1-2s if the frag lands near the boundary",
            clip_ref="may30_captioned_v3.mp4",
            tags=["anchor", "killfeed_pop"],
        )
    """
    body: dict = {"game": game, "feedback": feedback, "rule": rule}
    if clip_ref:
        body["clip_ref"] = clip_ref
    if source_context:
        body["source_context"] = source_context
    if tags:
        body["tags"] = tags
    with _client() as c:
        r = c.post(f"{API}/mentor/record_correction", json=body)
        r.raise_for_status()
        return r.json()


@mcp.tool()
def mentor_recent_corrections(game: str, limit: int = 10) -> dict:
    """List the most recent editorial corrections for a game, newest
    first. Use this to audit the memory before it drives the next
    compile -- catches stale / conflicting rules that need pruning."""
    with _client() as c:
        r = c.get(
            f"{API}/mentor/recent_corrections",
            params={"game": game, "limit": limit},
        )
        r.raise_for_status()
        return r.json()


@mcp.tool()
def auto_cut_kills_from_source(
    source_path: str,
    include_clusters: bool = True,
    name_prefix: str | None = None,
) -> dict:
    """Auto-cut one sub-clip per Overwolf kill anchor, ready to compile.
    Zero LLM cost -- pure ground-truth + per-payoff-type buffers.

    Windows use per-type buffers so the payoff lands in the sweet spot:
    single kill = 3s pre / 2.5s post; ace = 6s pre / 4s post. Overlapping
    windows (adjacent kills in a burst) merge into one bigger window --
    an ace becomes one clip, not four.

    Empty `clips` when the source isn't dispatched (with `reason` set).
    In that case, fall back to `trim_source_to_clip` for hand-picked
    windows.

    Chains with `compile_custom_montage` for the full hands-off flow:

        cuts = auto_cut_kills_from_source(source_path=src)
        compile_custom_montage(
            clip_paths=[c["output_path"] for c in cuts["clips"]],
            game="Valorant",
        )
    """
    body = {"source_path": source_path, "include_clusters": include_clusters}
    if name_prefix:
        body["name_prefix"] = name_prefix
    with _client() as c:
        r = c.post(f"{API}/clips/auto_cut_kills", json=body)
        r.raise_for_status()
        return r.json()


@mcp.tool()
def caption_video(
    source_path: str,
    segments: list[dict],
    name: str | None = None,
    aspect: str = "9:16",
) -> dict:
    """Burn drawtext overlays onto ANY .mp4 file by path (no asset row
    required). Companion to `trim_source_to_clip` -- together they
    let you jump-cut a source into sub-clips, compile, and then
    overlay comedy captions on the final montage.

    `segments` is a list of dicts, each with:
      - `text`: the caption text
      - `start_seconds`, `end_seconds`: OUTPUT-timeline times (t=0 =
        first frame of `source_path`)
      - `style` (optional): {fontsize, color, y_position, ...}
      - `font_path` (optional): full path to a .ttf. For emoji glyphs
        pass Segoe UI Emoji on Windows: `C:/Windows/Fonts/seguiemj.ttf`.
        Renders as monochrome outlines which look cleaner over gameplay
        than color emoji anyway. Different segments at the same
        timestamp can use different fonts (emoji glyph + text side by
        side by placing them at slightly different X positions via
        the style's y_position and letting drawtext auto-center each).

    `aspect`: '9:16', '16:9', 'source' (no aspect change), or
    '9:16_hud' (HUD-preserving vertical; requires HUD_ASPECT_ENABLED).

    Output: `WORKSPACE/captions/<name>.mp4`. Sync -- returns the path
    when the encode finishes (~5-20s depending on clip length).
    """
    body = {
        "source_path": source_path,
        "segments": segments,
        "aspect": aspect,
    }
    if name:
        body["name"] = name
    with _client() as c:
        r = c.post(f"{API}/clips/caption_video", json=body, timeout=180.0)
        r.raise_for_status()
        return r.json()


@mcp.tool()
def add_sfx_at(
    source_path: str,
    sfx: list[dict],
    name: str | None = None,
) -> dict:
    """Layer one or more sound-effect one-shots onto any .mp4's audio
    track. Sync -- returns the output path when the encode finishes.

    `sfx` is a list of dicts:
      - `sfx_path`: absolute path to a .wav/.mp3/.ogg
      - `at_seconds`: when to trigger (source-timeline seconds)
      - `volume` (optional, default 1.0): 0.0-4.0 linear gain

    Example -- vine boom on the punchline of a comedy short:

        add_sfx_at(
            source_path="C:/...may30_captioned.mp4",
            sfx=[{
                "sfx_path": "C:/sfx/vine_boom.mp3",
                "at_seconds": 17.5,
                "volume": 1.2,
            }],
            name="may30_boom",
        )

    Chains with `trim_source_to_clip` + `compile_custom_montage` +
    `caption_video` for the full TikTok-style edit pipeline.

    Output: `WORKSPACE/sfx_overlays/<name>.mp4`. Video stream is
    stream-copied (no re-encode), audio is remixed via ffmpeg
    `adelay` + `amix`. Missing sfx files are skipped -- the encode
    still succeeds as long as at least one overlay is valid.
    """
    body = {
        "source_path": source_path,
        "sfx": sfx,
    }
    if name:
        body["name"] = name
    with _client() as c:
        r = c.post(f"{API}/sfx/overlay", json=body, timeout=180.0)
        r.raise_for_status()
        return r.json()


@mcp.tool()
def watch_montage(
    custom_name: str | None = None,
    montage_path: str | None = None,
    save_markdown: bool = True,
) -> dict:
    """Have Claude Haiku 4.5 WATCH a compiled montage end-to-end and
    return per-interval structured critique.

    This is the "did the montage actually work?" tool. Unlike the
    per-clip and whole-comp validators (which spot-check 8-15 frames),
    this panel-packs the ENTIRE compiled `.mp4` at 1 fps into 2x2
    mosaics (4 seconds per panel) and sends the whole sequence to
    the VLM in one call. The VLM returns:

      - `overall_verdict`: ship_it | needs_edits | start_over
      - `opener_strength`, `climax_strength`: 1-5 gut ratings
      - `pacing`: too_slow | just_right | too_fast
      - `top_recommendation`: the single biggest change
      - `flags`: list of `{start_s, end_s, category, why}` where
        category is one of dead_air, scoreboard, own_death,
        teammate_kill_only, payoff_lands, good_buildup, great_moment,
        abrupt_cut, boring_pacing.

    Args:
      - `custom_name`: name of a custom-compile output (e.g.
        `Valorant_best_of_v1`). Resolved to
        `WORKSPACE/shorts/_custom/<name>/<name>.mp4`.
      - `montage_path`: absolute path to any rendered `.mp4` on disk
        (overrides `custom_name` if both are set — but pass one, not
        both).
      - `save_markdown`: when True (default), also writes
        `critique.md` next to the montage.

    Cost / latency: ~$0.02 and ~5 seconds for a 60s montage
    (Haiku 4.5 vision @ 15 panels). Cheap enough to re-run after
    every edit.

    Returns the parsed critique dict + `panel_count` +
    `total_seconds`, or `error` if ffmpeg / VLM was unavailable.
    """
    if not custom_name and not montage_path:
        return {
            "status": "failed",
            "error": "provide custom_name or montage_path",
        }
    body: dict = {"save_markdown": save_markdown}
    if montage_path:
        body["montage_path"] = montage_path
    if custom_name:
        body["custom_name"] = custom_name
    with _client() as c:
        # This endpoint runs inline (no job queue) -- give it its own
        # bump above the default 30s client timeout so a slow model
        # response doesn't false-fail the tool.
        resp = c.post(f"{API}/shorts/watch", json=body, timeout=180.0)
        resp.raise_for_status()
        return resp.json()


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


@mcp.tool()
def story_list_requests(round: str) -> dict:
    """List the owner's story requests for a round (panel folder name).

    Returns `{round, requests: [{index, kind, at, ...}]}`. `index` is
    0-based and is what `story_remove_request` takes. Read-only.
    """
    with _client() as c:
        return c.get(f"{API}/story/{round}/requests").json()


@mcp.tool()
def story_add_request(
    round: str,
    kind: str,
    at: str,
    before_s: float | None = None,
    after_s: float | None = None,
    title: str | None = None,
    note: str | None = None,
    effect: str | None = None,
    start: str | None = None,
    end: str | None = None,
    params: dict | None = None,
) -> dict:
    """Record one owner request for a round's story. It is applied the next
    time the story renders (`story_render`); nothing renders here.

    `round` = panel folder name. `at` = the GAME clock the owner sees
    ("20:05", "1:02:05" or seconds), not recording time. `kind`:
    - `add_chapter`: a new chapter around `at` (`before_s` default 5,
      `after_s` default 20, optional `title`, `note`).
    - `extend`: grow the chapter at `at` by `before_s` / `after_s`.
    - `drop`: remove the chapter at `at`.
    - `effect`: `effect` is letterbox | slowmo | zoom | focus, over
      `start`..`end` (game clock; default the chapter around `at`),
      optional numeric/str `params`.

    Examples. "walk out of base at 20:05":
      story_add_request(round, "add_chapter", "20:05", title="Walking out of base")
    "make the last teamfight cinematic" (fight at 31:10): three calls, same window:
      story_add_request(round, "effect", "31:10", effect="letterbox", start="31:05", end="31:20")
      story_add_request(round, "effect", "31:10", effect="slowmo", start="31:08", end="31:14")
      story_add_request(round, "effect", "31:10", effect="focus", start="31:05", end="31:20")

    Returns the updated list with indexes. Invalid input comes back as an
    error detail (nothing is saved).
    """
    body: dict = {"kind": kind, "at": at}
    for key, val in (
        ("before_s", before_s),
        ("after_s", after_s),
        ("title", title),
        ("note", note),
        ("effect", effect),
        ("start", start),
        ("end", end),
        ("params", params),
    ):
        if val is not None:
            body[key] = val
    with _client() as c:
        return c.post(f"{API}/story/{round}/requests", json=body).json()


@mcp.tool()
def story_remove_request(round: str, index: int) -> dict:
    """Remove one story request by its 0-based `index` (see
    `story_list_requests`). Indexes shift after a removal, so list again
    before removing a second one. Returns the updated list."""
    with _client() as c:
        return c.delete(f"{API}/story/{round}/requests/{index}").json()


@mcp.tool()
def story_render(round: str, asset_id: str, plays: bool = True) -> dict:
    """Re-render a round's story with its current requests applied (same as
    the `story-render` CLI). `plays=True` builds chapters from the frozen
    plays file; False uses the panel beats. Heavy ffmpeg work: one at a time.

    Job-based; this tool waits for completion. On success `output_path` is
    the story video. On timeout the job continues in the background.
    """
    with _client() as c:
        r = c.post(f"{API}/story/{round}/render", json={"asset_id": asset_id, "plays": plays})
        if r.status_code != 200:
            return {"status": "failed", "error": r.text}
        return _wait_for_job(c, r.json()["job_id"], timeout_s=1800)


@mcp.tool()
def story_suggest_edits(round: str) -> dict:
    """Get editor suggestions for a rendered story: 3-6 effects
    (letterbox_focus, slowmo, zoom) on its best moments. The gaming, viewer
    and editing voices are asked as typed questions through Jev (free rules
    when Jev is not configured); no Anthropic call. Needs the round's story
    rendered first (`story_render`). Replaces earlier suggestions.

    Job-based; this tool waits, then returns the suggestions:
    `{status, items: [{id, kind, chapter_index, start_s, end_s, params,
    reason, play_score, pop_p, backend, accepted}]}` with game-clock times.
    Next: show them to the owner, `story_accept_suggestion` the ones they
    want, then `story_apply_suggestions`."""
    with _client() as c:
        r = c.post(f"{API}/story/{round}/suggestions")
        if r.status_code != 200:
            return {"status": "failed", "error": r.text}
        job = _wait_for_job(c, r.json()["job_id"], timeout_s=600)
        if job.get("status") != "completed":
            return job
        return c.get(f"{API}/story/{round}/suggestions").json()


@mcp.tool()
def story_list_suggestions(round: str) -> dict:
    """Show a round's editor suggestions and which the owner accepted
    (`accepted`: true / false / null = undecided). `status` is pending
    (job running), ready, applied or failed (`error` says why). Read-only."""
    with _client() as c:
        return c.get(f"{API}/story/{round}/suggestions").json()


@mcp.tool()
def story_accept_suggestion(round: str, item_id: str, accepted: bool | None) -> dict:
    """Tick (`accepted=True`), reject (`False`) or clear (`None`) one
    suggestion by its `id` from `story_list_suggestions`. Nothing renders
    here. Returns the updated suggestions."""
    with _client() as c:
        return c.put(
            f"{API}/story/{round}/suggestions/{item_id}", json={"accepted": accepted}
        ).json()


@mcp.tool()
def story_apply_suggestions(round: str) -> dict:
    """Apply the accepted suggestions: each becomes `effect` request(s) in the
    round's requests (letterbox_focus = letterbox + focus over the same span;
    re-applying never duplicates), then the story re-renders. Heavy ffmpeg
    work: one at a time. Narration takes recorded before this go stale.

    Job-based; this tool waits for the render. On success `output_path` is
    the story video. On timeout the job continues in the background."""
    with _client() as c:
        r = c.post(f"{API}/story/{round}/suggestions/apply")
        if r.status_code != 200:
            return {"status": "failed", "error": r.text}
        return _wait_for_job(c, r.json()["job_id"], timeout_s=1800)


@mcp.tool()
def narration_status(kind: str, key: str) -> dict:
    """Show a video's narration: takes (id, start_s, duration_s, has_camera),
    camera box, `stale` (the video changed after the takes were recorded) and
    the video URL. `kind` is the narratable type ("story"); `key` is its id
    (for a story, the round folder name). Creates an empty session on first call.

    Recording itself is browser-only: tell the owner to open
    `/narrate/<kind>/<key>` in the web app, record the takes there, then use
    `narration_export` here."""
    with _client() as c:
        return c.get(f"{API}/narration/{kind}/{key}").json()


@mcp.tool()
def narration_set_camera(
    kind: str,
    key: str,
    enabled: bool,
    width_frac: float | None = None,
    x: float | None = None,
    y: float | None = None,
) -> dict:
    """Show or hide the camera box in the narrated export, and optionally
    move/resize it. `width_frac` is the box width as a fraction of the frame
    (0.08-0.5); `x`/`y` are the box's top-left corner as frame fractions
    (0-1). Anything left out keeps the game profile default. Returns the
    updated session (with the resolved `camera_px`)."""
    body: dict = {"enabled": enabled}
    for name, value in (("width_frac", width_frac), ("x", x), ("y", y)):
        if value is not None:
            body[name] = value
    with _client() as c:
        return c.put(f"{API}/narration/{kind}/{key}/camera", json=body).json()


@mcp.tool()
def narration_remove_take(kind: str, key: str, take_id: str) -> dict:
    """Delete one recorded take by `take_id` (see `narration_status`).
    Returns the updated session."""
    with _client() as c:
        return c.delete(f"{API}/narration/{kind}/{key}/takes/{take_id}").json()


@mcp.tool()
def narration_export(kind: str, key: str, force: bool = False) -> dict:
    """Mix the recorded takes over the video into `narrated.mp4` (original
    audio ducks while the owner talks; camera box only if enabled). Refuses
    when the video changed after recording (`stale`) unless `force=True`;
    to keep the takes as they are, ask the owner to re-lock in the web page.

    Job-based; this tool waits for completion. On success `output_path` is the
    narrated video. On timeout the job continues in the background."""
    with _client() as c:
        r = c.post(f"{API}/narration/{kind}/{key}/export", json={"force": force})
        if r.status_code != 200:
            return {"status": "failed", "error": r.text}
        return _wait_for_job(c, r.json()["job_id"], timeout_s=1800)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
