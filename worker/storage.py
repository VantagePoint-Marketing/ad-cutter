"""The private file bucket (Railway Storage Bucket, S3-compatible). Credentials come from environment variables that
Railway provides by reference: BUCKET, ACCESS_KEY_ID, SECRET_ACCESS_KEY, ENDPOINT, REGION.

Keys are laid out per job so one job can never touch another's files:
    uploads/<job_id>/source          one raw video (the staging command-line tools)
    uploads/<job_id>/clip-<n>        the raw clips the web page uploads, n from 0, in the order the person gave them
    results/<job_id>/<file>          finished MP4s and review notes (written by the worker)

The browser never sees the bucket credentials: the web page hands it short-lived signed links, one key each.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_SAFE = re.compile(r"^[A-Za-z0-9._ ()-]{1,120}$")
_JOB_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_CLIPS = 10


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

    # ---------------------------------------------------------------- key layout

    @staticmethod
    def upload_key(job_id: str) -> str:
        return f"uploads/{job_id}/source"

    @staticmethod
    def clip_key(job_id: str, n: int) -> str:
        if not 0 <= int(n) < MAX_CLIPS:
            raise ValueError(f"clip number {n} is out of range (0 to {MAX_CLIPS - 1})")
        return f"uploads/{job_id}/clip-{int(n)}"

    @staticmethod
    def owns(job_id: str, key: str) -> bool:
        """True when `key` is one of this job's own upload keys. The worker checks every source key against this,
        so a job row can never point the worker at another job's files."""
        if not _JOB_ID.match(job_id or ""):
            return False
        return key == Bucket.upload_key(job_id) or bool(re.fullmatch(rf"uploads/{job_id}/clip-[0-9]", key))

    @staticmethod
    def result_key(job_id: str, filename: str) -> str:
        if not _SAFE.match(filename) or filename.startswith("."):
            raise ValueError(f"unsafe result file name: {filename!r}")
        return f"results/{job_id}/{filename}"

    # ---------------------------------------------------------------- objects

    def size(self, key: str) -> int | None:
        """The object's size in bytes, or None when there is no such object."""
        from botocore.exceptions import ClientError
        try:
            return int(self.s3.head_object(Bucket=self.name, Key=key)["ContentLength"])
        except ClientError as err:
            if str(err.response.get("Error", {}).get("Code")) in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

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

    # ---------------------------------------------------------------- signed links for the browser

    def put_url(self, key: str, expires: int = 3600) -> str:
        """A link the browser can PUT one file to, for `expires` seconds. It grants nothing else: one key, one
        method, and it does not reveal the credentials."""
        return self.s3.generate_presigned_url("put_object", Params={"Bucket": self.name, "Key": key},
                                              ExpiresIn=expires, HttpMethod="PUT")

    def get_url(self, key: str, filename: str | None = None, attachment: bool = False, expires: int = 3600) -> str:
        """A link that serves one object for `expires` seconds, inline (video preview) or as a download."""
        params = {"Bucket": self.name, "Key": key}
        if filename:
            if not _SAFE.match(filename):
                raise ValueError(f"unsafe file name: {filename!r}")
            params["ResponseContentDisposition"] = f'{"attachment" if attachment else "inline"}; filename="{filename}"'
        return self.s3.generate_presigned_url("get_object", Params=params, ExpiresIn=expires)

    def allow_browser_uploads(self, origins: list[str]) -> None:
        """Let pages served from `origins` PUT files straight into the bucket (a CORS rule; replaces any earlier
        rule). The web app applies this when it starts, with its own public address."""
        self.s3.put_bucket_cors(Bucket=self.name, CORSConfiguration={"CORSRules": [
            {"AllowedHeaders": ["*"], "AllowedMethods": ["PUT"], "AllowedOrigins": list(origins),
             "ExposeHeaders": ["ETag"], "MaxAgeSeconds": 3000}]})
