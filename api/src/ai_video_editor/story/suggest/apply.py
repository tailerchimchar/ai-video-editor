"""Apply: the owner's accepted suggestions become `effect` requests in
`requests.json` (story/requests.py is the only way effects are written), then
the set is marked `applied`. Re-applying never duplicates a request."""

from __future__ import annotations

from pathlib import Path

from ..requests import Effect, load_requests, save_requests
from .models import SuggestionItem, SuggestionSet
from .store import SuggestionStore, UnknownSuggestionError

# kind -> renderer effects over the same span (the first one takes the params)
_EFFECTS = {
    "letterbox_focus": ("letterbox", "focus"),
    "slowmo": ("slowmo",),
    "zoom": ("zoom",),
}


def to_effects(item: SuggestionItem) -> list[Effect]:
    at = round((item.start_s + item.end_s) / 2, 2)
    return [
        Effect(
            at=at,
            effect=kind,
            start=item.start_s,
            end=item.end_s,
            params=dict(item.params) if n == 0 else {},
        )
        for n, kind in enumerate(_EFFECTS[item.kind])
    ]


def apply_accepted(story_dir: Path, store: SuggestionStore) -> tuple[SuggestionSet, int]:
    """Returns the updated set and how many requests were added. Raises
    UnknownSuggestionError without suggestions, ValueError for a bad
    requests.json."""
    current = store.load()
    if current is None:
        raise UnknownSuggestionError("no suggestions for this round yet")
    reqs = load_requests(story_dir)
    seen = [r.model_dump(mode="json") for r in reqs.requests]
    added = 0
    for item in (i for i in current.items if i.accepted):
        for effect in to_effects(item):
            dumped = effect.model_dump(mode="json")
            if dumped not in seen:
                reqs.requests.append(effect)
                seen.append(dumped)
                added += 1
    save_requests(story_dir, reqs)
    updated = current.model_copy(update={"status": "applied"})
    store.save(updated)
    return updated, added
