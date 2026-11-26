"""riot-hud-sync: measure the recording offset from the HUD K/D/A counter.

Dry run by default; `--write` stores it with offset_source="hud".
"""

from __future__ import annotations

import argparse
import dataclasses
import sys

from .hud_sync import calibrate_from_hud
from .riot_timeline import record_offset


def _default_load(asset_id: str):
    from ..story.cli import load_timeline

    return load_timeline(asset_id)


def _default_path(asset_id: str):
    from ..story.cli import asset_path

    return asset_path(asset_id)


def main(
    argv: list[str] | None = None,
    *,
    load=_default_load,
    path=_default_path,
    calibrate=calibrate_from_hud,
    record=record_offset,
) -> int:
    ap = argparse.ArgumentParser(prog="riot-hud-sync", description=__doc__.splitlines()[0])
    ap.add_argument("--asset", required=True, help="asset id")
    ap.add_argument("--source", help="video to read (default: the asset's path)")
    ap.add_argument("--write", action="store_true", help="store the result (offset_source=hud)")
    ap.add_argument(
        "--guess",
        type=float,
        help="search around this offset instead of the stored one (the search "
        "window is +-8 s, so a stored offset far off finds nothing); read it "
        "off one frame: recording second minus the HUD game clock",
    )
    args = ap.parse_args(argv)

    timeline = load(args.asset)
    if timeline is None:
        print(f"no cached Riot timeline for {args.asset}", file=sys.stderr)
        return 2
    src = args.source or path(args.asset)
    if not src:
        print(f"no video path for {args.asset}; pass --source", file=sys.stderr)
        return 2

    print(f"old offset: {timeline.offset_s:.2f} s ({timeline.offset_source})")
    if args.guess is not None:
        timeline = dataclasses.replace(timeline, offset_s=args.guess)
        print(f"searching around {args.guess:.2f} s")
    result = calibrate(src, timeline)
    if result is None:
        print("no reliable HUD measurement (fewer than 3 clean samples or spread > 0.5 s)")
        return 1
    print(f"new offset: {result.offset_s:.2f} s  n={result.n}  spread={result.spread_s:.2f} s")
    for t_game, off in result.samples:
        print(f"  game {t_game:.1f} s -> offset {off:.2f} s")
    if args.write:
        record(args.asset, result.offset_s, source="hud")
        print("written (offset_source=hud)")
    else:
        print("dry run: nothing written (use --write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
