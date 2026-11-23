"""league-understand: run the understanding pass for one asset and print the plays."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date, datetime
from pathlib import Path

from ..config import settings
from ..montage.month_pool import pool_game
from ..story.cli import asset_path, load_timeline
from .play_vocab import load_vocab
from .understand import understand_game


def _fmt(v: float | None) -> str:
    return "?" if v is None else f"{v:.0f}"


def recording_month(asset_id: str, video_path: str) -> str:
    """YYYY-MM of the recording: the asset's `created_at`, else the file's
    mtime, else today."""
    try:
        conn = sqlite3.connect(settings.database_path)
        try:
            row = conn.execute("SELECT created_at FROM assets WHERE id = ?", (asset_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        row = None
    if row and row[0]:
        try:
            return datetime.fromisoformat(str(row[0]).replace("Z", "+00:00")).strftime("%Y-%m")
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(Path(video_path).stat().st_mtime).strftime("%Y-%m")
    except OSError:
        return date.today().strftime("%Y-%m")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="league-understand", description=__doc__)
    ap.add_argument("--asset", required=True, help="asset id")
    ap.add_argument("--source", help="recording path (default: the asset's path)")
    ap.add_argument("--force", action="store_true", help="ignore the frozen plays file")
    ap.add_argument("--fps", type=float, default=settings.play_confirm_fps)
    ap.add_argument("--no-sweep", action="store_true", help="skip the kill-less sweep")
    ap.add_argument("--no-pool", action="store_true", help="do not cut plays into the month pool")
    args = ap.parse_args(argv)

    timeline = load_timeline(args.asset)
    if timeline is None:
        print(f"no cached Riot timeline for {args.asset}")
        return 1
    video = args.source or asset_path(args.asset)
    if not video:
        print(f"no recording path for {args.asset}; pass --source")
        return 1

    model = understand_game(
        args.asset,
        video,
        timeline,
        vocab=load_vocab("league"),
        budget_usd=settings.game_understanding_budget_usd,
        fps=args.fps,
        sweep_fps=settings.play_sweep_fps,
        sweep=settings.play_sweep_enabled and not args.no_sweep,
        force=args.force,
    )
    if model.phases is not None:
        ph = model.phases
        print(f"phases early<{ph.early.end_s:.0f} mid<{ph.mid.end_s:.0f}", end="")
        print(f" late>={ph.late.start_s:.0f}" if ph.late else "")
    for p in model.plays:
        w = p.window
        numbers = p.sides.numbers_confirmed or p.sides.numbers
        print(
            f"{p.id} {p.type} {p.phase} {p.zone_relative} {numbers} {p.owner.role} "
            f"{p.result.outcome} {_fmt(w.approach_s)}-{_fmt(w.engage_s)}-{_fmt(w.outcome_s)} "
            f"{w.source}"
        )
    print(f"budget_spent_usd {model.budget_spent_usd:.4f}")
    if model.budget_exhausted:
        print("budget_exhausted: the pass stopped early; raise GAME_UNDERSTANDING_BUDGET_USD")
    if not args.no_pool:
        month = recording_month(args.asset, video)
        pooled = pool_game(args.asset, model.plays, video, month=month)
        print(f"month_pool {month}: {len(pooled)} play(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
