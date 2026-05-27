"""Phonetic post-correction for Whisper output.

Snap mangled tokens back to their canonical spelling using rapidfuzz.
Used by `transcribe.py` after Whisper returns to fix the residual
errors that the `initial_prompt` bias couldn't catch (Whisper has
already decided on a token; we second-guess only when our profile
vocab has a very close phonetic match).

Pure functions — no I/O, no global state.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz, process

# Tokens that aren't worth fuzzy-matching: too short to phonetically
# resemble a vocab entry meaningfully, and matching aggressively here
# leads to common english words ("am", "in", "is") getting yanked
# toward proper nouns.
_MIN_TOKEN_LEN = 4

# Token splitter that preserves apostrophes (Kha'Zix, K'Sante, Vel'Koz).
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z']*")


def _build_lookup(vocabulary: list[str]) -> dict[str, str]:
    """Map lowercased vocab entries → canonical-cased entries.

    Multi-word entries (e.g. 'Master Yi', 'Lee Sin') are stored as
    their first word for per-token matching. Falls back to whole
    string match where the token has no spaces.
    """
    out: dict[str, str] = {}
    for v in vocabulary:
        head = v.split()[0]
        out[head.lower()] = head
    return out


def _build_alias_lookup(aliases: dict[str, list[str]] | None) -> dict[str, str]:
    """Invert {canonical: [alias1, alias2]} → {alias_lower: canonical}.

    Aliases bypass the fuzz threshold — used for proper nouns Whisper
    consistently mishears AS valid English words (e.g. 'Noodlz' →
    'noodles'). Threshold-based matching won't catch those because the
    mishear IS already a real word.
    """
    out: dict[str, str] = {}
    if not aliases:
        return out
    for canonical, alts in aliases.items():
        for alt in alts:
            out[alt.lower()] = canonical
    return out


def fuzzy_correct(
    text: str,
    vocabulary: list[str],
    threshold: int = 80,
    aliases: dict[str, list[str]] | None = None,
) -> str:
    """Return `text` with tokens snapped to nearby vocab entries.

    Two layers:
      1. **Direct aliases** (`aliases` arg): exact lowercase match against
         a hand-curated alias list snaps unconditionally to the canonical.
         Use when the mishear is itself a valid English word (e.g.
         'noodles' → 'Noodlz') and the fuzz threshold can't disambiguate.
      2. **Fuzzy match** (`vocabulary` + `threshold`): rapidfuzz WRatio.
         80 catches edit-distance-1 typos and stem variants
         ('yasso'→'Yasuo') but spares clean English. Multi-token mishears
         ('yes so'→'Yasuo') aren't catchable here — those rely on
         Whisper's `initial_prompt` bias to win on the first pass.

    Tokens shorter than _MIN_TOKEN_LEN aren't candidates (signal/noise
    is too low for short tokens) UNLESS they appear in the alias map.

    Pure. Leaves punctuation, casing of non-matched words, and
    whitespace untouched.
    """
    if not vocabulary and not aliases:
        return text

    canonicals = _build_lookup(vocabulary)
    alias_lookup = _build_alias_lookup(aliases)
    keys = list(canonicals.keys())

    def _replace(match: re.Match) -> str:
        tok = match.group(0)
        tok_lower = tok.lower()
        # 1. Direct alias match takes precedence — bypasses both the
        # length check and the fuzz threshold.
        if tok_lower in alias_lookup:
            return alias_lookup[tok_lower]
        if len(tok) < _MIN_TOKEN_LEN or tok_lower in canonicals:
            # already canonical (case-insensitive) or too short to fuzz-match
            return canonicals.get(tok_lower, tok)
        if not keys:
            return tok
        result = process.extractOne(
            tok_lower,
            keys,
            scorer=fuzz.WRatio,
            score_cutoff=threshold,
        )
        if result is None:
            return tok
        matched_key, _score, _i = result
        return canonicals[matched_key]

    return _TOKEN_RE.sub(_replace, text)
