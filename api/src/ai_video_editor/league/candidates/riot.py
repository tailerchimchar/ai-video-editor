"""Riot API candidate source (League of Legends).

Outplayed/audio give us *approximate* moments; Riot's MATCH-V5 timeline
gives us *exact* kill timestamps (game-clock ms).

Correlation (two stages, both pure & testable):
- pick the match: convert the Outplayed *filename's* local record-start
  (DST-aware via `recording_timezone`) to UTC and pick the match with
  the closest game start (`_correlate`). st_ctime is NOT used — it is
  unrelated to record time on many systems.
- score honesty: `_confidence` reports high/medium/low from the start &
  duration deltas. A low-confidence pick is surfaced, never hidden — the
  caller/folder must not trust it.
- map kills: a full recording spans the match, so a kill at game-clock
  T maps to offset ≈ T (`_kill_candidates`), tunable via
  `RIOT_SYNC_OFFSET_SECONDS`.

Fully config-gated and non-fatal: if unconfigured, not League, the
filename has no parseable time, or any API error occurs → returns []
so the other sources still work.

Endpoints used (Riot ACCOUNT-V1 / MATCH-V5, regional routing):
  GET /riot/account/v1/accounts/by-riot-id/{name}/{tag}
  GET /lol/match/v5/matches/by-puuid/{puuid}/ids
  GET /lol/match/v5/matches/{id}
  GET /lol/match/v5/matches/{id}/timeline
"""

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from ...config import settings

_TIMEOUT = 10.0
_log = logging.getLogger(__name__)

# Outplayed filenames: "<game>_MM-DD-YYYY_H-M-S-ms.mp4" — a local
# wall-clock stamp (empirically either record-start or finalize; see
# _correlate, which tries both). Still far better than st_ctime, which
# is unrelated to record time on many systems.
_FN_TIME_RE = re.compile(r"_(\d{1,2})-(\d{1,2})-(\d{4})_(\d{1,2})-(\d{1,2})-(\d{1,2})")


def _expected_start_ms(filename: str) -> int | None:
    """Local record-start from the filename → UTC epoch ms (DST-aware)."""
    m = _FN_TIME_RE.search(filename)
    if not m:
        return None
    mo, d, y, h, mi, s = (int(x) for x in m.groups())
    try:
        local = datetime(y, mo, d, h, mi, s, tzinfo=ZoneInfo(settings.recording_timezone))
    except (ValueError, KeyError):
        return None
    return int(local.timestamp() * 1000)


def _correlate(fn_ms: int, rec_dur_ms: int, matches: list[dict]) -> tuple[dict | None, int, int]:
    """Pick the match whose game start is closest to the recording.

    The Outplayed filename timestamp is empirically inconsistent — on
    some recordings it's the record *start*, on others the *finalize*
    (≈ start + duration). Measuring real data showed no constant offset,
    so we test both anchors and keep the closest match. The returned
    `delta_start_ms` (on the winning anchor) stays the honesty signal:
    if both anchors are far off, it's still LOW and flagged, never hidden.

    Returns (match | None, delta_start_ms, delta_dur_ms).
    """
    anchors = (fn_ms, fn_ms - rec_dur_ms)  # start-anchored, finalize-anchored
    best, best_ds, best_dd = None, 0, 0
    for m in matches:
        g_start, g_end = _game_window_ms(m)
        ds = min(abs(g_start - a) for a in anchors)
        dd = abs((g_end - g_start) - rec_dur_ms)
        if best is None or ds < best_ds:
            best, best_ds, best_dd = m, ds, dd
    return best, best_ds, best_dd


def _confidence(delta_start_ms: int, delta_dur_ms: int) -> str:
    ds, dd = abs(delta_start_ms) / 1000, abs(delta_dur_ms) / 1000
    if ds <= 300 and dd <= 180:
        return "high"
    if ds <= 900:
        return "medium"
    return "low"


def _is_league(asset: dict) -> bool:
    hay = f"{asset.get('game') or ''} {asset.get('filename') or ''}".lower()
    return "league of legends" in hay


def _configured() -> bool:
    return bool(settings.riot_api_key and settings.riot_id and "#" in settings.riot_id)


def _base() -> str:
    return f"https://{settings.riot_region}.api.riotgames.com"


def _get(client: httpx.Client, path: str, **params) -> dict | list:
    r = client.get(
        _base() + path,
        params=params or None,
        headers={"X-Riot-Token": settings.riot_api_key},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


# --- pure correlation helpers (no I/O) ---
def _game_window_ms(match_info: dict) -> tuple[int, int]:
    """(start_ms, end_ms) for a MATCH-V5 match `info` object, handling the
    historical gameDuration units quirk."""
    info = match_info["info"]
    start = int(info["gameStartTimestamp"])
    end = info.get("gameEndTimestamp")
    if end is not None:
        return start, int(end)
    dur = int(info.get("gameDuration", 0))
    # Pre-patch-11.20 matches report gameDuration in ms; later ones in s.
    dur_ms = dur if dur > 100_000 else dur * 1000
    return start, start + dur_ms


def _our_participant(match_info: dict, puuid: str) -> dict | None:
    for p in match_info["info"].get("participants", []):
        if p.get("puuid") == puuid:
            return p
    return None


# MATCH-V5 timeline event types we read. CHAMPION_SPECIAL_KILL "upgrades"
# the matching CHAMPION_KILL's event_type rather than emitting a separate
# candidate (avoids duplicate clips at the same moment); ELITE_MONSTER_KILL
# is its own candidate (objectives are independent moments).
_MULTI_TO_TAG = {2: "double_kill", 3: "triple_kill", 4: "quadra_kill", 5: "penta_kill"}
_MONSTER_TO_TAG = {
    "BARON_NASHOR": "objective_baron",
    "DRAGON": "objective_dragon",
    "RIFTHERALD": "objective_herald",
}
_UPGRADE_MATCH_WINDOW_MS = 5000  # multikill markers fire within ~5s of the underlying kill


def _iter_events(timeline: dict):
    """Flatten the timeline's frame.events into a single iterable."""
    for frame in timeline.get("info", {}).get("frames", []):
        yield from frame.get("events", [])


def _event_candidates(
    match_info: dict,
    timeline: dict,
    puuid: str,
    duration_s: float,
    pad: float,
    sync_offset_s: float = 0.0,
    corr: dict | None = None,
) -> list[dict]:
    """Map our timeline events (kills, multikills, first blood, objectives)
    to recording-relative windows.

    A full Outplayed game recording spans the match (recording length ≈
    game length), so an event at game-clock T sits at ≈ offset T in the
    recording. `sync_offset_s` tunes any constant record-start lead/lag.

    Event taxonomy emitted:
      - `kill` / `death`           — CHAMPION_KILL where I'm killer/victim
      - `first_blood`              — CHAMPION_KILL upgraded by a matching
                                     CHAMPION_SPECIAL_KILL killType=KILL_FIRST_BLOOD
      - `double_kill` … `penta_kill` — CHAMPION_KILL upgraded by a matching
                                     CHAMPION_SPECIAL_KILL killType=KILL_MULTI
      - `ace`                      — CHAMPION_KILL upgraded by KILL_ACE
                                     (only when I got the ace-securing kill)
      - `objective_baron` /
        `objective_dragon` /
        `objective_herald`         — ELITE_MONSTER_KILL where I'm the killer

    Multikill / first-blood markers from Riot fire alongside the underlying
    CHAMPION_KILL (within ~5s). We *upgrade the kill's event_type* instead
    of emitting a duplicate candidate so the highlights folder and
    compilation see one clip per moment, not two.
    """
    me = _our_participant(match_info, puuid)
    if me is None:
        return []
    pid = me.get("participantId")
    champion = me.get("championName")
    match_id = match_info.get("metadata", {}).get("matchId")

    def _row(event_type: str, riot_ms: int, extra_meta: dict | None = None) -> dict | None:
        offset = riot_ms / 1000.0 + sync_offset_s
        if offset < 0 or offset > duration_s:
            return None
        start = max(0.0, offset - pad)
        end = min(duration_s, offset + pad)
        meta = {
            "match_id": match_id,
            "champion": champion,
            "riot_event_ms": riot_ms,
            "anchor_seconds": round(offset, 2),
            "correlation_confidence": (corr or {}).get("confidence"),
            "delta_start_seconds": (corr or {}).get("delta_start_s"),
            "delta_duration_seconds": (corr or {}).get("delta_dur_s"),
        }
        if extra_meta:
            meta.update(extra_meta)
        return {
            "start_seconds": round(start, 2),
            "end_seconds": round(end, 2),
            "confidence": 0.95,
            "event_type": event_type,
            "metadata": meta,
        }

    # Pass 1: CHAMPION_KILL events involving us.
    kills: list[dict] = []  # each row keeps `metadata.riot_event_ms` for pass-2 matching
    for ev in _iter_events(timeline):
        if ev.get("type") != "CHAMPION_KILL":
            continue
        if ev.get("killerId") == pid:
            kind = "kill"
        elif ev.get("victimId") == pid:
            kind = "death"
        else:
            continue
        row = _row(kind, int(ev["timestamp"]), {"rationale": "Riot MATCH-V5 CHAMPION_KILL"})
        if row is not None:
            kills.append(row)

    # Pass 2: CHAMPION_SPECIAL_KILL by us upgrades the matching kill.
    # We mutate in place to avoid duplicate rows for the same moment.
    for ev in _iter_events(timeline):
        if ev.get("type") != "CHAMPION_SPECIAL_KILL" or ev.get("killerId") != pid:
            continue
        sk_ms = int(ev.get("timestamp", 0))
        kill_type = ev.get("killType")
        if kill_type == "KILL_FIRST_BLOOD":
            tag = "first_blood"
        elif kill_type == "KILL_MULTI":
            length = int(ev.get("multiKillLength", 2))
            tag = _MULTI_TO_TAG.get(length)
        elif kill_type == "KILL_ACE":
            tag = "ace"
        else:
            tag = None
        if tag is None:
            continue
        # Match to the nearest of our kills within the window. Special
        # kills fire *after* the underlying CHAMPION_KILL, so we prefer
        # an earlier kill if equidistant.
        best_i = None
        best_d = _UPGRADE_MATCH_WINDOW_MS + 1
        for i, k in enumerate(kills):
            if k["event_type"] not in {"kill", "first_blood", *_MULTI_TO_TAG.values(), "ace"}:
                continue
            d = abs(k["metadata"]["riot_event_ms"] - sk_ms)
            if d < best_d:
                best_d = d
                best_i = i
        if best_i is not None and best_d <= _UPGRADE_MATCH_WINDOW_MS:
            kills[best_i]["event_type"] = tag
            kills[best_i]["metadata"]["rationale"] = f"Riot CHAMPION_SPECIAL_KILL ({kill_type})"

    # Pass 3: objectives we personally took.
    objectives: list[dict] = []
    for ev in _iter_events(timeline):
        if ev.get("type") != "ELITE_MONSTER_KILL" or ev.get("killerId") != pid:
            continue
        mtype = ev.get("monsterType")
        tag = _MONSTER_TO_TAG.get(mtype)
        if tag is None:
            continue
        extra = {"rationale": f"Riot ELITE_MONSTER_KILL ({mtype})"}
        sub = ev.get("monsterSubType")
        if sub:
            extra["monster_subtype"] = sub
        row = _row(tag, int(ev["timestamp"]), extra)
        if row is not None:
            objectives.append(row)

    return sorted(kills + objectives, key=lambda r: r["start_seconds"])


# Backwards-compat alias — kept while tests / callers migrate.
_kill_candidates = _event_candidates


# --- public, fully-gated entrypoint ---
# Status codes distinguish "nothing because honestly no match" from
# "nothing because the API was throttled/erroring" — the latter is a
# transient degradation worth surfacing & retrying, not a true negative.
RiotStatus = str  # "ok" | "no_match" | "rate_limited" | "api_error" | "disabled"


def detect_riot_events(asset: dict, duration: float) -> tuple[list[dict], RiotStatus]:
    """Return (candidates, status) for a League recording.

    Always non-fatal (other sources still run), but the status makes the
    *reason* for an empty result explicit so callers can tell a real
    "no confident match" from a rate-limit/API failure.
    """
    if not (_configured() and _is_league(asset) and duration > 0):
        return [], "disabled"

    duration_ms = int(duration * 1000)
    expected_start_ms = _expected_start_ms(asset.get("filename", ""))
    if expected_start_ms is None:
        return [], "disabled"  # no parseable filename time → can't correlate

    name, _, tag = settings.riot_id.partition("#")
    try:
        with httpx.Client() as client:
            acct = _get(client, f"/riot/account/v1/accounts/by-riot-id/{name}/{tag}")
            puuid = acct["puuid"]
            match_ids = _get(
                client,
                f"/lol/match/v5/matches/by-puuid/{puuid}/ids",
                start=0,
                count=settings.riot_match_lookback,
            )
            matches = [_get(client, f"/lol/match/v5/matches/{mid}") for mid in match_ids]
            chosen, ds_ms, dd_ms = _correlate(expected_start_ms, duration_ms, matches)
            if chosen is None:
                return [], "no_match"
            corr = {
                "confidence": _confidence(ds_ms, dd_ms),
                "delta_start_s": round(ds_ms / 1000, 1),
                "delta_dur_s": round(dd_ms / 1000, 1),
            }
            match_id = chosen["metadata"]["matchId"]
            timeline = _get(client, f"/lol/match/v5/matches/{match_id}/timeline")
            result = _kill_candidates(
                chosen,
                timeline,
                puuid,
                duration,
                settings.analyze_window_padding,
                settings.riot_sync_offset_seconds,
                corr,
            )
            return result, ("ok" if result else "no_match")
    except httpx.HTTPStatusError as e:
        status = "rate_limited" if e.response.status_code == 429 else "api_error"
        _log.warning("Riot source skipped (%s): %s", status, e)
        return [], status
    except Exception as e:  # network/parse/etc. — still non-fatal
        _log.warning("Riot source skipped (api_error): %s", e)
        return [], "api_error"

    return result
