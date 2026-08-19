"""Private PDF storage boundary for S3-compatible object storage."""

from __future__ import annotations

import os
from datetime import datetime, timezone

_client = None


def _s3_client():
    global _client
    if not os.getenv("OBJECT_STORAGE_BUCKET", "").strip():
        return None
    if _client is None:
        import boto3

        kwargs = {"region_name": os.getenv("OBJECT_STORAGE_REGION", "auto")}
        endpoint = os.getenv("OBJECT_STORAGE_ENDPOINT", "").strip()
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        access_key = os.getenv("OBJECT_STORAGE_ACCESS_KEY", "").strip()
        secret_key = os.getenv("OBJECT_STORAGE_SECRET_KEY", "").strip()
        if access_key and secret_key:
            kwargs.update(aws_access_key_id=access_key, aws_secret_access_key=secret_key)
        _client = boto3.client("s3", **kwargs)
    return _client


def put_pdf(user_id: int, document_id: int, filename: str, raw: bytes) -> str | None:
    client = _s3_client()
    if client is None:
        return None
    key = f"users/{user_id}/documents/{document_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.pdf"
    client.put_object(Bucket=os.environ["OBJECT_STORAGE_BUCKET"], Key=key, Body=raw, ContentType="application/pdf", ServerSideEncryption="AES256")
    return key


def delete_pdf(key: str | None) -> None:
    if not key:
        return
    client = _s3_client()
    if client is not None:
        client.delete_object(Bucket=os.environ["OBJECT_STORAGE_BUCKET"], Key=key)
