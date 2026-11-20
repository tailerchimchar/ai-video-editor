"""Publish one panel round to the Grok team: digest + clips on Drive, one event.

This is the producer the bots depend on: it fills the ids they must write
into queue items (`compilation_id`, `compilation_version`, `asset_id`) and
the Drive ids of the digest and of every clip. Run it after each panel
round when the Grok team is on:

    uv run grokbot-publish-round <round_dir> --game league [--compilation-id X]

Privacy gate: when the round has a `transcript.json`, a digest that leaks
any transcript fragment is refused before anything is uploaded. A clip file
outside WORKSPACE_DIR / OUTPLAYED_MEDIA_DIR is never uploaded.

Upload gate: if any clip upload or the digest upload fails when an uploader
is configured, `round.published` is never sent. Instead `round.publish_failed`
is emitted so consumers know the round is incomplete.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path

from .. import notify
from ..config import settings
from ..notify import Event
from ..taxonomy import Game
from .digest import Digest, _ref_from_file, annotate_with_jev, build_digest, digest_leaks
from .drive import Uploader
from .paths import inside

logger = logging.getLogger(__name__)

CLIP_PADDING_SECONDS = 10.0


def _default_player_names_path() -> Path:
    """Return the conventional repo-relative path for the player names file.

    Resolved relative to this module's location (api/src/.../grokbot/publish.py)
    to find api/player-names.txt regardless of cwd.
    """
    # publish.py is at api/src/ai_video_editor/grokbot/publish.py
    # We want api/player-names.txt (4 levels up)
    return Path(__file__).parent.parent.parent.parent / "player-names.txt"


def _load_player_names(path: Path) -> tuple[set[str], str | None]:
    """Load player names from a local file (one name per line).

    Returns (names, error_reason). When the file is missing or unreadable,
    returns (empty set, reason) to block publishing (fail closed).
    """
    if not path.is_file():
        logger.error(
            "publish: player names file %s not found, blocking publish (fail closed)", path
        )
        return set(), "names_file_missing"
    try:
        text = path.read_text(encoding="utf-8")
        names = {line.strip().casefold() for line in text.splitlines() if line.strip()}
        logger.info("publish: loaded %d player names from %s", len(names), path)
        return names, None
    except OSError as e:
        logger.error("publish: could not read player names file %s: %s", path, e)
        return set(), "names_file_unreadable"


def _digest_contains_player_name(digest: Digest, names: set[str]) -> list[str]:
    """Check if any player name appears in the digest text fields.

    Returns the list of matched names (empty if no match).
    """
    if not names:
        return []

    def _strings_from(value) -> list[str]:
        if isinstance(value, str):
            return [value.casefold()]
        if isinstance(value, dict):
            return [s for v in value.values() for s in _strings_from(v)]
        if isinstance(value, list | tuple):
            return [s for v in value for s in _strings_from(v)]
        return []

    haystacks = _strings_from(digest.model_dump())
    matched = []
    for name in names:
        if any(name in h for h in haystacks):
            matched.append(name)
    return matched


class DigestLeakError(RuntimeError):
    """The digest contains transcript text; nothing was uploaded."""


class PlayerNameLeakError(RuntimeError):
    """The digest contains a player name from the local names list; nothing was uploaded."""


class PublishUploadError(RuntimeError):
    """An upload failed; round.published was not sent."""

    def __init__(self, round_name: str, failed_clips: list[str], digest_failed: bool):
        self.round_name = round_name
        self.failed_clips = failed_clips
        self.digest_failed = digest_failed
        parts = []
        if failed_clips:
            parts.append(f"clips: {failed_clips}")
        if digest_failed:
            parts.append("digest upload failed")
        super().__init__(f"round {round_name}: {'; '.join(parts)}")


class PlayerNamesFileError(RuntimeError):
    """The player names file is missing or unreadable; publishing blocked."""

    def __init__(self, path: Path, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"player names file {path}: {reason}")


def _default_ask_many() -> Callable | None:
    if settings.decisions_backend == "rules":
        return None
    from ..decisions import ask_many

    return ask_many


def _clip_path(round_dir: Path, file: str) -> Path:
    p = Path(file)
    return p if p.is_absolute() else round_dir / p


def _upload(
    uploader: Uploader | None, path: Path, name: str, subfolder: str
) -> tuple[str | None, bool]:
    """Upload a file to Drive.

    Returns (file_id, success). When uploader is None, returns (None, True) since
    no upload was intended. A missing file is (None, True) since the file simply
    doesn't exist. An actual upload failure is (None, False).
    """
    if uploader is None:
        return None, True
    if not path.is_file():
        logger.warning("publish: %s is missing, not uploaded", path)
        return None, True
    try:
        file_id = uploader.upload(path, name=name, subfolder=subfolder)
        logger.info("publish: uploaded %s to %s -> %s", name, subfolder, file_id)
        return file_id, True
    except Exception as e:
        logger.warning("publish: upload of %s failed: %s", name, e)
        return None, False


def _clip_files(round_dir: Path) -> dict[str, Path]:
    """`ref -> clip file` from clips.json (the same refs the digest uses)."""
    try:
        clips = json.loads((round_dir / "clips.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("publish: cannot read clips.json: %s", e)
        return {}
    out: dict[str, Path] = {}
    for clip in clips if isinstance(clips, list) else []:
        file = clip.get("file") if isinstance(clip, dict) else None
        if isinstance(file, str) and file:
            out[_ref_from_file(file)] = _clip_path(round_dir, file)
    return out


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
    player_names_path: Path | None = None,
) -> dict:
    """Build, gate, upload and announce one round. Returns the event payload.

    `ask_many`: None picks the default (Jev via `decisions.ask_many` unless
    the decisions backend is "rules"); False skips annotation; a callable
    is used as given. `api` (a runner `ApiClient`) supplies the
    compilation's current plan version when `compilation_id` is given.

    `player_names_path`: path to a local file with player names (one per line).
    Behavior depends on `name_check_mode`:
    - "off": skip the check entirely, file not required
    - "warn": check if file exists, log warning on match, publish anyway
    - "block": block on match (exit 3) or missing/unreadable file (exit 5)
    When None, defaults to the conventional repo-relative path (api/player-names.txt).

    `name_check_mode`: "off", "warn", or "block". Defaults to settings value.

    Raises:
        DigestLeakError: If transcript text appears in the digest.
        PlayerNameLeakError: If a player name appears in the digest (block mode only).
        PlayerNamesFileError: If the file is missing/unreadable (block mode only).
        PublishUploadError: If an upload fails when an uploader is configured.
    """
    round_dir = Path(round_dir)

    # Determine name check mode from settings if not explicitly passed
    check_mode = settings.grokbot_player_name_check

    # Privacy gate 2 (early): player names check based on mode
    player_names: set[str] = set()
    if check_mode != "off":
        names_path = player_names_path
        if names_path is None:
            names_path = _default_player_names_path()

        player_names, file_error = _load_player_names(names_path)
        if file_error is not None:
            if check_mode == "block":
                failure_payload = {
                    "round": round_dir.name,
                    "step": "names_file",
                    "reason": file_error,
                    "failed_clips": [],
                    "digest_failed": False,
                }
                logger.error(
                    "publish: round %s blocked - player names file error: %s",
                    round_dir.name,
                    file_error,
                )
                emit(Event(event="round.publish_failed", payload=failure_payload))
                raise PlayerNamesFileError(names_path, file_error)
            else:  # warn mode: log and continue without names
                logger.warning(
                    "publish: player names file %s: %s (continuing without name check)",
                    names_path,
                    file_error,
                )

    digest: Digest = build_digest(round_dir, game=game)
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

    # Privacy gate 1: transcript text
    transcript = round_dir / "transcript.json"
    if transcript.is_file():
        leaks = digest_leaks(digest, transcript)
        if leaks:
            raise DigestLeakError(f"digest leaks {len(leaks)} transcript fragment(s)")

    # Privacy gate 2 (content): check digest against loaded names
    if check_mode != "off" and player_names:
        name_matches = _digest_contains_player_name(digest, player_names)
        if name_matches:
            if check_mode == "block":
                raise PlayerNameLeakError(
                    f"digest contains {len(name_matches)} player name(s): blocked before upload"
                )
            else:  # warn mode
                logger.warning(
                    "publish: round %s contains %d player name match(es) (warn mode, continuing)",
                    round_dir.name,
                    len(name_matches),
                )

    files = _clip_files(round_dir)
    roots = (settings.workspace_dir, settings.outplayed_media_dir)
    clip_ids: dict[str, str | None] = {}
    failed_clips: list[str] = []
    skipped = 0

    for clip in digest.clips:
        path = files.get(clip.ref)
        if path is not None and not inside(path, *roots):
            logger.warning("publish: %s is outside the allowed roots, not uploaded", path)
            skipped += 1
            path = None
        if path:
            drive_name = f"{round_dir.name}-{clip.ref}.mp4"
            file_id, success = _upload(uploader, path, drive_name, "clips")
            clip.drive = file_id
            if not success:
                failed_clips.append(clip.ref)
        else:
            clip.drive = None
        clip_ids[clip.ref] = clip.drive

    local = round_dir / "digest.json"
    local.write_text(digest.model_dump_json(indent=2), encoding="utf-8")
    digest_id, digest_ok = _upload(uploader, local, f"{round_dir.name}-digest.json", "digests")

    # If uploader is configured and any upload failed, emit failure event
    if uploader is not None and (failed_clips or not digest_ok):
        failure_payload = {
            "round": round_dir.name,
            "step": "digest" if not digest_ok else "clips",
            "reason": "upload failed",
            "failed_clips": failed_clips,
            "digest_failed": not digest_ok,
        }
        logger.error(
            "publish: round %s failed - clips: %s, digest: %s",
            round_dir.name,
            failed_clips,
            "failed" if not digest_ok else "ok",
        )
        emit(Event(event="round.publish_failed", payload=failure_payload))
        raise PublishUploadError(round_dir.name, failed_clips, not digest_ok)

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
        "--player-names",
        type=Path,
        default=None,
        help="path to a local file with player names (one per line) to block from digest",
    )
    args = parser.parse_args(argv)

    if not (args.round_dir / "clips.json").is_file():
        print(f"{args.round_dir} has no clips.json", file=sys.stderr)
        sys.exit(1)

    from .drive import uploader_from_settings
    from .runner import HttpApiClient

    # Player names path: CLI arg overrides env/config, which overrides default
    names_path = args.player_names
    if names_path is None and settings.grokbot_player_names_path:
        names_path = Path(settings.grokbot_player_names_path)
    # If still None, publish_round will use _default_player_names_path()

    api = HttpApiClient(settings.grokbot_api_base_url) if args.compilation_id else None
    try:
        result = publish_round(
            args.round_dir,
            game=args.game,
            compilation_id=args.compilation_id,
            asset_id=args.asset_id,
            api=api,
            uploader=uploader_from_settings(),
            ask_many=False if args.no_jev else None,
            player_names_path=names_path,
        )
    except DigestLeakError as e:
        print(f"refused: {e}", file=sys.stderr)
        sys.exit(2)
    except PlayerNameLeakError as e:
        print(f"refused: {e}", file=sys.stderr)
        sys.exit(3)
    except PublishUploadError as e:
        print(f"upload failed: {e}", file=sys.stderr)
        sys.exit(4)
    except PlayerNamesFileError as e:
        print(f"names file error: {e}", file=sys.stderr)
        _flush_webhook()  # flush the round.publish_failed event
        sys.exit(5)
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
