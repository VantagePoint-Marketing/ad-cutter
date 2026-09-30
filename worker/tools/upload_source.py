"""Upload a raw video from this PC to the media bucket as a new job's source, for staging tests.

    railway run -p <project id> -e staging -s worker -- python worker/tools/upload_source.py "<path to video>"

`railway run` gives this process the worker's bucket variables (BUCKET, ENDPOINT, REGION, ACCESS_KEY_ID,
SECRET_ACCESS_KEY); nothing secret is printed. The file lands at uploads/<job id>/source and the job id is printed.
Then queue it inside the container:
    railway ssh -- sh -c 'cd /app/worker && python tools/jobctl.py enqueue --job-id <job id> --source-name <file name>'
"""
from __future__ import annotations

import os
import sys
import threading
import uuid
from pathlib import Path

import boto3
from botocore.config import Config

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import Bucket  # noqa: E402  (same key layout as the worker)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    size = path.stat().st_size
    job_id = str(uuid.uuid4())
    key = Bucket.upload_key(job_id)
    s3 = boto3.client("s3", endpoint_url=os.environ["ENDPOINT"], region_name=os.environ.get("REGION", "auto"),
                      aws_access_key_id=os.environ["ACCESS_KEY_ID"], aws_secret_access_key=os.environ["SECRET_ACCESS_KEY"],
                      config=Config(s3={"addressing_style": "virtual"}, retries={"max_attempts": 5}))
    sent, lock, shown = [0], threading.Lock(), [-1]

    def progress(n: int) -> None:
        with lock:
            sent[0] += n
            pct = sent[0] * 10 // size
            if pct != shown[0]:
                shown[0] = pct
                print(f"  {pct * 10}%", flush=True)

    content_type = "video/quicktime" if path.suffix.lower() == ".mov" else "video/mp4"
    print(f"uploading {path.name} ({size / 1024 ** 2:.0f} MB) to {os.environ['BUCKET']}/{key}", flush=True)
    s3.upload_file(str(path), os.environ["BUCKET"], key, ExtraArgs={"ContentType": content_type}, Callback=progress)
    head = s3.head_object(Bucket=os.environ["BUCKET"], Key=key)
    if head["ContentLength"] != size:
        print(f"size mismatch after upload: {head['ContentLength']} != {size}")
        return 1
    print(f"done. job id: {job_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
