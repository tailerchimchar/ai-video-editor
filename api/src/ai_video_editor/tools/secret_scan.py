"""Refuse to let a key leave the machine: scan text and files for secret-like strings.

    uv run secret-scan [paths...]

With no paths it scans the branch diff against origin/master plus the
workspace's decisions, riot and panel digests. Exit 1 on any hit; values
are never printed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

from ..config import settings

PATTERNS: dict[str, re.Pattern] = {
    "anthropic": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "riot": re.compile(r"RGAPI-[0-9a-f-]{30,}"),
    "google_secret": re.compile(r"GOCSPX-[A-Za-z0-9_-]{10,}"),
    "google_refresh": re.compile(r"1//0[A-Za-z0-9_-]{30,}"),
    "github_pat_classic": re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    "github_pat": re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    "langfuse_public": re.compile(r"pk-lf-[a-f0-9-]{20,}"),
    "langfuse_secret": re.compile(r"sk-lf-[a-f0-9-]{20,}"),
    "xai": re.compile(r"xai-[A-Za-z0-9]{30,}"),
    "typesafe": re.compile(r"(?<![A-Za-z0-9])ts_[A-Za-z0-9]{24,}"),
}
# Any long token on a line that mentions TYPESAFE is treated as a TypeSafe key.
_TYPESAFE_LINE = re.compile(r"(?<![A-Za-z0-9_/\\.-])[A-Za-z0-9_-]{40,}(?![A-Za-z0-9_/\\.-])")
_TYPESAFE_WORD = re.compile(r"(?i)typesafe")


class Hit(NamedTuple):
    path: Path
    line: int
    pattern: str


def scan_text(text: str) -> list[tuple[str, int]]:
    """(pattern name, 1-based line number) for every secret-like string in `text`."""
    hits: list[tuple[str, int]] = []
    for n, line in enumerate(text.splitlines(), start=1):
        for name, pat in PATTERNS.items():
            if pat.search(line):
                hits.append((name, n))
        if (
            _TYPESAFE_WORD.search(line)
            and _TYPESAFE_LINE.search(line)
            and ("typesafe", n) not in hits
        ):
            hits.append(("typesafe", n))
    return hits


def _is_env(path: Path) -> bool:
    return path.name == ".env" or path.name.startswith(".env.")


def _files(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(sorted(f for f in p.rglob("*") if f.is_file()))
        elif p.is_file():
            out.append(p)
    return out


def scan_paths(paths: list[Path]) -> list[Hit]:
    """Scan files (directories are walked). Missing paths, `.env` files and binaries are skipped."""
    hits: list[Hit] = []
    for f in _files([Path(p) for p in paths]):
        if _is_env(f):
            continue
        try:
            raw = f.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:8192]:
            continue
        for name, line in scan_text(raw.decode("utf-8", errors="replace")):
            hits.append(Hit(f, line, name))
    return hits


def _diff_added_text() -> str:
    try:
        r = subprocess.run(
            ["git", "diff", "origin/master...HEAD"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return "\n".join(
        ln[1:] for ln in r.stdout.splitlines() if ln.startswith("+") and not ln.startswith("+++")
    )


def _default_paths() -> list[Path]:
    ws = settings.workspace_dir
    paths = [ws / "decisions", ws / "riot"]
    panel = ws / "panel"
    if panel.is_dir():
        paths.extend(sorted(panel.glob("*/digest.json")))
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="secret-scan", description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args(argv)

    hits = scan_paths(args.paths or _default_paths())
    if not args.paths:
        diff = _diff_added_text()
        if diff:
            with tempfile.TemporaryDirectory() as d:
                f = Path(d) / "git-diff"
                f.write_text(diff, encoding="utf-8")
                hits += [Hit(Path("git diff"), h.line, h.pattern) for h in scan_paths([f])]
    for h in hits:
        print(f"{h.path}:{h.line} {h.pattern} [value masked]")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
