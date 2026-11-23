"""Precision gate CLI.

    league-plays-eval --asset ID [--labels PATH] [--fps 0.25,1.0 --source PATH]

Without --fps: matches the cached GameModel (WORKSPACE/plays/<asset>.json)
against the labels. With --fps: re-runs understand_game per fps (force) and
prints one line each: fps, cost, precision, recall, gate."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..config import settings
from .play_models import GameModel
from .plays_eval import EvalReport, Labels, labels_path, load_labels, match_plays, passes_gate


def _print_report(report: EvalReport) -> None:
    print(f"precision {report.precision:.2f}  recall {report.recall:.2f}")
    print("type              prec  rec  labels  detected")
    for t, m in report.per_type.items():
        print(
            f"{t:<17} {m['precision']:.2f}  {m['recall']:.2f}  "
            f"{int(m['n_labels']):>6}  {int(m['n_detected']):>8}"
        )
    for lab_type, det_type in report.type_confusion:
        print(f"confusion: label {lab_type} detected as {det_type}")
    print(f"gate (>=0.8): {'PASS' if passes_gate(report) else 'FAIL'}")


def _run_fps(asset: str, fps_list: list[float], source: str, labels: Labels) -> int:
    from .play_vocab import load_vocab
    from .riot_timeline import cache_dir_for, load_cached, parse_timeline
    from .understand import understand_game

    cached = load_cached(cache_dir_for(asset))
    if cached is None:
        print(f"no usable Riot cache for {asset}")
        return 2
    match, raw, meta = cached
    timeline = parse_timeline(
        match,
        raw,
        owner_puuid=meta["owner_puuid"],
        offset_s=float(meta.get("offset_s") or 0.0),
        confidence=meta["confidence"],
    )
    for f in fps_list:
        model = understand_game(
            asset,
            source,
            timeline,
            vocab=load_vocab(),
            budget_usd=settings.game_understanding_budget_usd,
            fps=f,
            sweep_fps=min(f, settings.play_sweep_fps),
            sweep=settings.play_sweep_enabled,
            force=True,
            out_path=Path(settings.workspace_dir) / "plays" / f"{asset}.fps{f}.json",
        )
        rep = match_plays(model.plays, labels.rows)
        print(
            f"fps {f}  cost ${model.budget_spent_usd:.2f}  precision {rep.precision:.2f}  "
            f"recall {rep.recall:.2f}  gate {'PASS' if passes_gate(rep) else 'FAIL'}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="league-plays-eval", description=__doc__)
    parser.add_argument("--asset", required=True)
    parser.add_argument("--labels", default=None, help="default WORKSPACE/labels/<asset>.json")
    parser.add_argument("--fps", default=None, help="comma list, e.g. 0.25,1.0")
    parser.add_argument("--source", default=None, help="recording path (default: assets table)")
    args = parser.parse_args(argv)

    lpath = Path(args.labels) if args.labels else labels_path(args.asset)
    if not lpath.is_file():
        print(f"labels not found: {lpath}")
        return 2
    labels = load_labels(lpath)

    if args.fps:
        source = args.source
        if source is None:
            from ..story.cli import asset_path

            source = asset_path(args.asset)
        if source is None:
            print("recording path unknown: pass --source")
            return 2
        return _run_fps(args.asset, [float(x) for x in args.fps.split(",")], source, labels)

    cache = Path(settings.workspace_dir) / "plays" / f"{args.asset}.json"
    if not cache.is_file():
        print(f"no game model at {cache}")
        return 2
    model = GameModel.model_validate_json(cache.read_text(encoding="utf-8"))
    _print_report(match_plays(model.plays, labels.rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
