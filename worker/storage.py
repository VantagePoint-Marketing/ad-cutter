"""The private file bucket (Railway Storage Bucket, S3-compatible). Credentials come from environment variables that
Railway provides by reference: BUCKET, ACCESS_KEY_ID, SECRET_ACCESS_KEY, ENDPOINT, REGION.

Keys are laid out per job so one job can never touch another's files:
    uploads/<job_id>/source          the raw video (written by the web app via signed multipart upload)
    results/<job_id>/<file>          finished MP4s and review notes (written by the worker)
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_SAFE = re.compile(r"^[A-Za-z0-9._ ()-]{1,120}$")


def _need(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"{name} is not set (Railway bucket variable)")
    return v


class Bucket:
    def __init__(self, client=None, bucket: str | None = None):
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("s3", endpoint_url=_need("ENDPOINT"), region_name=os.environ.get("REGION", "auto"),
                                  aws_access_key_id=_need("ACCESS_KEY_ID"),
                                  aws_secret_access_key=_need("SECRET_ACCESS_KEY"),
                                  config=Config(s3={"addressing_style": "virtual"}, retries={"max_attempts": 5}))
        self.s3, self.name = client, bucket or _need("BUCKET")

    @staticmethod
    def upload_key(job_id: str) -> str:
        return f"uploads/{job_id}/source"

    @staticmethod
    def result_key(job_id: str, filename: str) -> str:
        if not _SAFE.match(filename) or filename.startswith("."):
            raise ValueError(f"unsafe result file name: {filename!r}")
        return f"results/{job_id}/{filename}"

    def download(self, key: str, dest: Path, max_bytes: int = 4 * 1024 ** 3) -> Path:
        head = self.s3.head_object(Bucket=self.name, Key=key)
        if head["ContentLength"] > max_bytes:
            raise ValueError(f"upload is {head['ContentLength'] / 1024 ** 3:.1f} GB; the limit is 4 GB")
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.s3.download_file(self.name, key, str(dest))
        return dest

    def upload(self, path: Path, key: str, content_type: str) -> str:
        self.s3.upload_file(str(path), self.name, key, ExtraArgs={"ContentType": content_type})
        return key
