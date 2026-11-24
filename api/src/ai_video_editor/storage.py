"""Cloud copies of hand-picked raw VODs in Supabase Storage (S3-compatible API).

Nothing uploads automatically: the owner picks a recording and it is copied
into the private `outplayed` bucket. The object key is the file's path
relative to OUTPLAYED_MEDIA_DIR (POSIX separators), so the bucket keeps the
same folder layout as the disk:

    OUTPLAYED_MEDIA_DIR/League of Legends/x/y.mp4 -> outplayed:League of Legends/x/y.mp4

Local files are only read, never modified or deleted. A self-imposed budget
(STORAGE_MAX_TOTAL_GB, summed over the managed buckets) refuses an upload
that would exceed it.

Pure core (`vod_key`, `projected_total`, `over_budget`) is separated from
the boto3 shell so it can be tested without network.
"""

from __future__ import annotations

import json
import mimetypes
import re
from datetime import datetime
from pathlib import Path

from .config import settings

# Outplayed stamps the local start time into every file name.
_FN_TIME_RE = re.compile(r"_(\d{1,2})-(\d{1,2})-(\d{4})_(\d{1,2})-(\d{1,2})-(\d{1,2})")
META_SUFFIX = ".meta.json"

# Multipart above 64 MiB, in 64 MiB parts (Supabase S3 allows 5 MiB-5 GiB parts).
_PART_BYTES = 64 * 1024 * 1024


def is_configured() -> bool:
    return bool(
        settings.supabase_s3_endpoint
        and settings.supabase_s3_access_key_id
        and settings.supabase_s3_secret_access_key
    )


def managed_buckets() -> list[str]:
    """Buckets counted against the budget."""
    return [settings.storage_outplayed_bucket, settings.storage_workspace_bucket]


def vod_key(path: Path, root: Path | None = None) -> str:
    """Bucket key for a recording: its path relative to the Outplayed root.

    Raises ValueError for a path outside the root (never upload arbitrary files).
    """
    root = (root or settings.outplayed_media_dir).resolve()
    return path.resolve().relative_to(root).as_posix()


def local_path_for_key(key: str, root: Path | None = None) -> Path:
    """Where a pulled VOD lands: the same relative path under the Outplayed root.

    Raises ValueError for a key that would escape the root (`..`, absolute).
    """
    root = (root or settings.outplayed_media_dir).resolve()
    dest = (root / key).resolve()
    dest.relative_to(root)  # raises ValueError when outside
    if dest == root:
        raise ValueError("empty key")
    return dest


def meta_key(key: str) -> str:
    """The label sidecar that sits next to a VOD in the bucket."""
    return key + META_SUFFIX


def is_meta_key(key: str) -> bool:
    return key.endswith(META_SUFFIX)


def recorded_at_from_name(name: str) -> str | None:
    """`League of Legends_11-23-2026_21-30-50-0.mp4` -> `2026-11-23T21:30:50` (local)."""
    m = _FN_TIME_RE.search(name)
    if not m:
        return None
    mo, d, y, h, mi, s = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi, s).isoformat()
    except ValueError:
        return None


def _owner_from_match(match: dict, puuid: str | None) -> dict | None:
    for p in match.get("info", {}).get("participants", []):
        if puuid and p.get("puuid") == puuid:
            return p
    return None


def vod_label(asset: dict, *, riot_dir: Path | None = None) -> dict:
    """What the cloud copy is, in plain fields: the recording (asset id, file,
    session folder, local start time, length) plus, when the Riot cache for
    the asset exists, the game (champion, K/D/A, result, match id, patch,
    queue, sync offset). Never includes a puuid or Riot ID. `label` is the
    one-line summary shown in listings."""
    name = str(asset.get("filename") or Path(str(asset.get("path", ""))).name)
    dur = asset.get("duration_seconds")
    out: dict = {
        "asset_id": asset.get("id"),
        "filename": name,
        "session": asset.get("game"),
        "recorded_at": recorded_at_from_name(name),
        "duration_s": round(float(dur), 1) if dur else None,
    }
    game: dict | None = None
    if riot_dir is not None and (riot_dir / "match.json").is_file():
        try:
            match = json.loads((riot_dir / "match.json").read_text(encoding="utf-8"))
            meta: dict = {}
            if (riot_dir / "meta.json").is_file():
                meta = json.loads((riot_dir / "meta.json").read_text(encoding="utf-8"))
            info = match.get("info", {})
            me = _owner_from_match(match, meta.get("owner_puuid")) or {}
            game = {
                "match_id": meta.get("match_id") or match.get("metadata", {}).get("matchId"),
                "champion": me.get("championName"),
                "kills": me.get("kills"),
                "deaths": me.get("deaths"),
                "assists": me.get("assists"),
                "win": me.get("win"),
                "game_duration_s": info.get("gameDuration"),
                "game_version": info.get("gameVersion"),
                "queue_id": info.get("queueId"),
                "offset_s": meta.get("offset_s"),
                "offset_confidence": meta.get("confidence"),
            }
        except (OSError, ValueError):
            game = None
    out["game"] = game
    date = (out["recorded_at"] or "")[:10]
    mins = f"{out['duration_s'] / 60:.0f} min" if out["duration_s"] else ""
    if game and game.get("champion"):
        result = "WIN" if game.get("win") else "LOSS"
        kda = f"{game.get('kills')}/{game.get('deaths')}/{game.get('assists')}"
        head = f"{game['champion']} {kda} {result}"
    else:
        head = name
    out["label"] = " \u00b7 ".join(x for x in (head, mins, date) if x)
    return out


def merge_labels(objects: list[dict], labels: dict[str, dict | None]) -> list[dict]:
    """VOD objects only (sidecars dropped), each with its `label` sidecar (or None)."""
    return [{**o, "label": labels.get(o["key"])} for o in objects if not is_meta_key(o["key"])]


def projected_total(stored_bytes: int, replaced_bytes: int, upload_bytes: int) -> int:
    """Total after an upload: what's stored, minus an object being overwritten, plus the upload."""
    return stored_bytes - replaced_bytes + upload_bytes


def over_budget(total_bytes: int, max_gb: float) -> bool:
    return total_bytes > max_gb * 1024**3


# --- I/O shell -------------------------------------------------------------


def client():
    if not is_configured():
        raise RuntimeError(
            "Supabase storage not configured: set SUPABASE_S3_ENDPOINT, "
            "SUPABASE_S3_ACCESS_KEY_ID and SUPABASE_S3_SECRET_ACCESS_KEY in .env"
        )
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.supabase_s3_endpoint,
        region_name=settings.supabase_s3_region,
        aws_access_key_id=settings.supabase_s3_access_key_id,
        aws_secret_access_key=settings.supabase_s3_secret_access_key,
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 5}),
    )


def list_objects(s3, bucket: str) -> list[dict]:
    out: list[dict] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            out.append(
                {
                    "key": obj["Key"],
                    "size": obj["Size"],
                    "last_modified": obj["LastModified"].isoformat(),
                }
            )
    return out


def stored_bytes(s3) -> int:
    return sum(o["size"] for b in managed_buckets() for o in list_objects(s3, b))


def remote_size(s3, bucket: str, key: str) -> int | None:
    from botocore.exceptions import ClientError

    try:
        return s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def upload_file(s3, bucket: str, key: str, path: Path) -> None:
    from boto3.s3.transfer import TransferConfig

    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    s3.upload_file(
        str(path),
        bucket,
        key,
        ExtraArgs={"ContentType": ctype},
        Config=TransferConfig(
            multipart_threshold=_PART_BYTES, multipart_chunksize=_PART_BYTES, max_concurrency=4
        ),
    )


def download_file(s3, bucket: str, key: str, dest: Path) -> None:
    """Download to `<dest>.part`, then rename — a scan never sees a half file."""
    from boto3.s3.transfer import TransferConfig

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    s3.download_file(
        bucket,
        key,
        str(tmp),
        Config=TransferConfig(
            multipart_threshold=_PART_BYTES, multipart_chunksize=_PART_BYTES, max_concurrency=4
        ),
    )
    tmp.replace(dest)


def delete_object(s3, bucket: str, key: str) -> None:
    s3.delete_object(Bucket=bucket, Key=key)


def put_json(s3, bucket: str, key: str, data: dict) -> None:
    body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")


def get_json(s3, bucket: str, key: str) -> dict | None:
    from botocore.exceptions import ClientError

    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
