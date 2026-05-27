"""League of Legends specialization.

A self-contained package for LoL-specific candidate sources and the
orchestrator that runs them. Imports flow strictly INTO this package
from shared modules (`profiles`, `config`, `models`); the rest of the
codebase only touches `league` through the single dispatch line in
`candidates.service.compute_candidates`.

Adding new LoL signals (CV champion ID, killfeed reader, etc.) means
adding files here, not modifying shared code.
"""

from __future__ import annotations


def is_league(game: str | None) -> bool:
    """True if `asset.game` refers to a League of Legends recording.

    `load_profile` handles the Outplayed `<name>_<date>` suffix
    internally — we just consult the resolved profile's canonical name.
    """
    if not game:
        return False
    from ..profiles import load_profile  # local import to avoid a cycle

    return load_profile(game).meta.game == "league"
