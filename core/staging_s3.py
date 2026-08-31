"""Short-lived S3-compatible staging for cross-machine file delivery."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4


@dataclass(frozen=True)
class StagedFile:
    key: str
    download_url: str
    expires_in_seconds: int


def staging_is_configured() -> bool:
    names = (
        "WORKSPACE_STAGING_S3_ENDPOINT",
        "WORKSPACE_STAGING_S3_BUCKET",
        "WORKSPACE_STAGING_S3_ACCESS_KEY_ID",
        "WORKSPACE_STAGING_S3_SECRET_ACCESS_KEY",
    )
    configured = [name for name in names if os.getenv(name)]
    if configured and len(configured) != len(names):
        missing = [name for name in names if name not in configured]
        raise ValueError(
            "Incomplete Workspace staging configuration; missing "
            + ", ".join(missing)
        )
    return bool(configured)


def _client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ["WORKSPACE_STAGING_S3_ENDPOINT"],
        region_name="auto",
        aws_access_key_id=os.environ["WORKSPACE_STAGING_S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["WORKSPACE_STAGING_S3_SECRET_ACCESS_KEY"],
    )


def _ttl_seconds() -> int:
    value = int(os.getenv("WORKSPACE_STAGING_S3_GET_TTL_SECONDS", "3600"))
    if not 1 <= value <= 604800:
        raise ValueError("WORKSPACE_STAGING_S3_GET_TTL_SECONDS must be 1..604800")
    return value


def _key(filename: str) -> str:
    day = datetime.now(UTC).date().isoformat()
    return f"google-workspace/{day}/{uuid4()}/{Path(filename).name}"


def stage_bytes(data: bytes, filename: str, mime_type: Optional[str]) -> StagedFile:
    client = _client()
    bucket = os.environ["WORKSPACE_STAGING_S3_BUCKET"]
    key = _key(filename)
    kwargs = {"Bucket": bucket, "Key": key, "Body": data}
    if mime_type:
        kwargs["ContentType"] = mime_type
    client.put_object(**kwargs)
    ttl = _ttl_seconds()
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=ttl,
    )
    return StagedFile(key=key, download_url=url, expires_in_seconds=ttl)


def stage_path(path: Path, filename: str, mime_type: Optional[str]) -> StagedFile:
    client = _client()
    bucket = os.environ["WORKSPACE_STAGING_S3_BUCKET"]
    key = _key(filename)
    kwargs = {"ExtraArgs": {"ContentType": mime_type}} if mime_type else {}
    client.upload_file(str(path), bucket, key, **kwargs)
    ttl = _ttl_seconds()
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=ttl,
    )
    return StagedFile(key=key, download_url=url, expires_in_seconds=ttl)
