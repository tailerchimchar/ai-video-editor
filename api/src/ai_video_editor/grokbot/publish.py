"""Publish one panel round to the Grok team: digest + clips on Drive, one event.

This is the producer the bots depend on: it fills the ids they must write
into queue items (`compilation_id`, `compilation_version`, `asset_id`) and
the Drive ids of the digest and of every clip. Run it after each panel
round when the Grok team is on:

    uv run grokbot-publish-round <round_dir> --game league [--compilation-id X]

Privacy gate: when the round has a `transcript.json`, a digest that leaks
any transcript fragment is refused before anything is uploaded. A clip file
outside WORKSPACE_DIR / OUTPLAYED_MEDIA_DIR is never uploaded.

With `--asset-id` the digest also gets the Riot timeline (events, game
state), a Haiku paraphrase of each clip's transcript slice (`--no-paraphrase`
skips it) and an energy label. Energy uses the window's mean transcript
arousal and its laughter tokens; loudness is not wired yet (None), so
`calm` comes from low arousal only and `panic` has no rule this round.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from .. import notify
from ..config import settings
from ..notify import Event
from ..taxonomy import Game
from ..tools.secret_scan import scan_paths
from .digest import Digest, _ref_from_file, annotate_with_jev, build_digest, digest_leaks
from .drive import Uploader
from .paths import inside
from .voice import energy_label, transcript_slice, voice_paraphrase, window_sentiment

if TYPE_CHECKING:
    from ..league.riot_timeline import RiotTimeline

logger = logging.getLogger(__name__)


class DigestLeakError(RuntimeError):
    """The digest contains transcript text; nothing was uploaded."""


class SecretLeakError(RuntimeError):
    """The digest contains a secret-like string; the digest was not uploaded."""


def _default_ask_many() -> Callable | None:
    if settings.decisions_backend == "rules":
        return None
    from ..decisions import ask_many

    return ask_many


_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _round_date(round_name: str) -> str:
    """Drive day folder for a round: its YYYY-MM-DD prefix, else today (UTC)."""
    m = _DATE_PREFIX.match(round_name)
    return m.group(1) if m else datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _clip_path(base_dir: Path, file: str) -> Path:
    p = Path(file)
    return p if p.is_absolute() else base_dir / p


def _upload(uploader: Uploader | None, path: Path, name: str, subfolder: str) -> str | None:
    if uploader is None:
        return None
    if not path.is_file():
        logger.warning("publish: %s is missing, not uploaded", path)
        return None
    try:
        return uploader.upload(path, name=name, subfolder=subfolder)
    except Exception as e:
        logger.warning("publish: upload of %s failed: %s", name, e)
        return None


def _clip_files(round_dir: Path, clips_dir: Path | None = None) -> dict[str, Path]:
    """`ref -> clip file` from clips.json (the same refs the digest uses).

    Relative `file` entries resolve against `clips_dir` (the highlights
    session folder the clips were cut into) or, when not given, the round dir.
    """
    base = clips_dir or round_dir
    try:
        clips = json.loads((round_dir / "clips.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("publish: cannot read clips.json: %s", e)
        return {}
    out: dict[str, Path] = {}
    for clip in clips if isinstance(clips, list) else []:
        file = clip.get("file") if isinstance(clip, dict) else None
        if isinstance(file, str) and file:
            out[_ref_from_file(file)] = _clip_path(base, file)
    return out


def _asset_row(asset_id: str) -> dict | None:
    if not Path(settings.database_path).is_file():
        return None  # never let sqlite3.connect create an empty DB
    conn = sqlite3.connect(settings.database_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def _load_timeline(asset_id: str, game: str) -> RiotTimeline | None:
    """The asset's Riot timeline (cached, else fetched); None on any failure."""
    if game != Game.league:
        return None
    from ..league import riot_timeline
    from ..league.candidates import riot

    def fetch():
        try:
            asset = _asset_row(asset_id)
            if asset is None:
                return None
            return riot.fetch_timeline_inputs(asset, float(asset.get("duration_seconds") or 0))
        except Exception as e:  # enrichment is optional
            logger.warning("publish: Riot timeline fetch failed: %s", e)
            return None

    try:
        return riot_timeline.load_or_fetch(asset_id, fetch=fetch)
    except Exception as e:
        logger.warning("publish: Riot timeline unavailable: %s", e)
        return None


def _digest_inputs(
    asset_id: str,
    game: str,
    *,
    paraphrase: bool = True,
    load_timeline: Callable[[str, str], RiotTimeline | None] | None = None,
) -> dict:
    """The digest's optional inputs for one asset, bound to the local DB."""
    db = settings.database_path
    return {
        "timeline": (load_timeline or _load_timeline)(asset_id, game),
        "transcript_slices": partial(transcript_slice, db, asset_id),
        "paraphrase": partial(voice_paraphrase, asset_id=asset_id) if paraphrase else None,
        # Loudness is not wired this round: arousal and laughter tokens only.
        "energy": lambda s, e: energy_label(
            None, window_sentiment(db, asset_id, s, e), transcript_slice(db, asset_id, s, e)
        ),
    }


def publish_round(
    round_dir: Path,
    *,
    game: str,
    compilation_id: str | None,
    api,
    uploader: Uploader | None,
    ask_many: Callable | bool | None = None,
    emit: Callable[[Event], None] = notify.emit,
    asset_id: str | None = None,
    clips_dir: Path | None = None,
    timeline: RiotTimeline | None = None,
    transcript_slices: Callable[[float, float], str] | None = None,
    paraphrase: Callable[[str], str] | None = None,
    energy: Callable[[float, float], str] | None = None,
) -> dict:
    """Build, gate, upload and announce one round. Returns the event payload.

    `ask_many`: None picks the default (Jev via `decisions.ask_many` unless
    the decisions backend is "rules"); False skips annotation; a callable
    is used as given. `api` (a runner `ApiClient`) supplies the
    compilation's current plan version when `compilation_id` is given.
    `timeline`, `transcript_slices`, `paraphrase` and `energy` go to
    `build_digest` as given (None = that enrichment is off).
    """
    round_dir = Path(round_dir)
    digest: Digest = build_digest(
        round_dir,
        game=game,
        timeline=timeline,
        transcript_slices=transcript_slices,
        paraphrase=paraphrase,
        energy=energy,
    )
    digest.compilation_id = compilation_id
    digest.asset_id = asset_id
    if compilation_id and api is not None:
        digest.compilation_version = str(api.plan_version(compilation_id))

    asker = _default_ask_many() if ask_many is None else (ask_many or None)
    if asker is not None and digest.clips:
        try:
            annotate_with_jev(digest, ask_many=asker)
        except Exception as e:  # enrichment is optional; the digest still ships
            logger.warning("publish: Jev annotation failed: %s", e)

    transcript = round_dir / "transcript.json"
    if transcript.is_file():
        leaks = digest_leaks(digest, transcript)
        if leaks:
            raise DigestLeakError(f"digest leaks {len(leaks)} transcript fragment(s)")

    # Scan the serialised digest before anything is uploaded.
    local = round_dir / "digest.json"
    local.write_text(digest.model_dump_json(indent=2), encoding="utf-8")
    hits = scan_paths([local])
    if hits:
        local.unlink(missing_ok=True)
        found = ", ".join(f"{h.pattern}@{h.line}" for h in hits)
        raise SecretLeakError(f"secret-like string in digest ({found})")

    files = _clip_files(round_dir, clips_dir)
    roots = (settings.workspace_dir, settings.outplayed_media_dir)
    clip_ids: dict[str, str | None] = {}
    skipped = 0
    for clip in digest.clips:
        path = files.get(clip.ref)
        if path is not None and not inside(path, *roots):
            # clips.json is not trusted to name only footage: never ship other files.
            logger.warning("publish: %s is outside the allowed roots, not uploaded", path)
            skipped += 1
            path = None
        clip.drive = _upload(uploader, path, path.name, f"clips/{round_dir.name}") if path else None
        clip_ids[clip.ref] = clip.drive

    local.write_text(digest.model_dump_json(indent=2), encoding="utf-8")
    digest_id = _upload(
        uploader, local, f"{round_dir.name}-digest.json", f"digests/{_round_date(round_dir.name)}"
    )

    payload = {
        "round": round_dir.name,
        "game": game,
        "asset_id": asset_id,
        "compilation_id": compilation_id,
        "compilation_version": digest.compilation_version,
        "drive": {"digest": digest_id, "clips": clip_ids},
        "clips": len(digest.clips),
        "skipped_outside_roots": skipped,
    }
    emit(Event(event="round.published", payload=payload))
    return payload


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="grokbot-publish-round",
        description="Publish a panel round's digest and clips to the Grok team's Drive.",
    )
    parser.add_argument("round_dir", type=Path)
    parser.add_argument("--game", required=True, choices=[g.value for g in Game])
    parser.add_argument("--compilation-id", default=None)
    parser.add_argument("--asset-id", default=None)
    parser.add_argument("--no-jev", action="store_true", help="skip the Jev annotation")
    parser.add_argument(
        "--no-paraphrase",
        action="store_true",
        help="skip the Haiku voice paraphrase (needs --asset-id to matter)",
    )
    parser.add_argument(
        "--clips-dir",
        type=Path,
        default=None,
        help="folder holding the clip files named in clips.json (default: the round dir)",
    )
    args = parser.parse_args(argv)

    if not (args.round_dir / "clips.json").is_file():
        print(f"{args.round_dir} has no clips.json", file=sys.stderr)
        sys.exit(1)

    from .drive import uploader_from_settings
    from .runner import HttpApiClient

    api = HttpApiClient(settings.grokbot_api_base_url) if args.compilation_id else None
    inputs = (
        _digest_inputs(args.asset_id, args.game, paraphrase=not args.no_paraphrase)
        if args.asset_id
        else {}
    )
    try:
        result = publish_round(
            args.round_dir,
            game=args.game,
            compilation_id=args.compilation_id,
            asset_id=args.asset_id,
            api=api,
            uploader=uploader_from_settings(),
            ask_many=False if args.no_jev else None,
            clips_dir=args.clips_dir,
            **inputs,
        )
    except DigestLeakError as e:
        print(f"refused: {e}", file=sys.stderr)
        sys.exit(2)
    except SecretLeakError as e:
        print(f"refused: {e}", file=sys.stderr)
        sys.exit(6)
    _flush_webhook()
    print(json.dumps(result, indent=2))


def _flush_webhook(timeout: float = 10.0) -> None:
    """A CLI exits right after emitting: let the webhook worker post first."""
    from ..notify import sinks

    sink = getattr(sinks, "_webhook", None)
    if sink is not None:
        sink.flush(timeout)


if __name__ == "__main__":
    main()
