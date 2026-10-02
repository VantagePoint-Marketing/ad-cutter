"""Treat every uploaded video as hostile, and keep secrets away from every child process.

- check_upload(): decides from the file's first bytes (not its name) whether it is an MP4/MOV or Matroska/WebM
  container, then probes it with that demuxer forced. Playlists (HLS), concat lists, images and anything else are
  rejected before ffmpeg can follow references inside them.
- ffmpeg_input(): the input options every ffmpeg call on an upload must use: the forced demuxer plus a protocol
  whitelist, so ffmpeg can't open network URLs.
- clean_env(): the environment for child processes (ffmpeg, ffprobe, npx/HyperFrames, Chromium). An allowlist, so
  API keys, database URLs and bucket credentials never reach them. HyperFrames reads OPENROUTER_API_KEY and
  GEMINI_API_KEY itself when they are present, so this matters beyond uploads.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

PROTOCOLS = "file"          # local files only: no network, no pipes
# ffprobe's format_name for each demuxer we allow
DEMUXERS = {"mov": "mov,mp4,m4a,3gp,3g2,mj2", "matroska": "matroska,webm"}
# first atom of real MP4/MOV files: ftyp (phones), styp/sidx/moof (fragmented), wide/mdat/free/skip/junk/uuid/pnot (older)
MOV_ATOMS = {b"ftyp", b"styp", b"sidx", b"moof", b"moov", b"mdat", b"wide", b"free", b"skip", b"junk", b"uuid", b"pnot"}
MAX_SIDE = 4096             # 4K phone footage fits; anything bigger is refused (decompression-bomb guard)
EBML_MAGIC = b"\x1a\x45\xdf\xa3"
# transfer functions of HDR video (iPhone HDR is HLG; some cameras and apps write PQ)
HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}

# Variables child processes may see. Everything else (keys, tokens, DB URLs, bucket credentials) is dropped.
_PASS = {
    # POSIX
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    "XDG_STATE_HOME", "FONTCONFIG_PATH", "FONTCONFIG_FILE",
    # Windows (local development runs)
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "HOMEDRIVE", "HOMEPATH", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "SYSTEMDRIVE",
    # HyperFrames / Node settings we set on purpose
    "HYPERFRAMES_BROWSER_PATH", "PRODUCER_HEADLESS_SHELL_PATH", "HYPERFRAMES_FONT_CACHE_DIR", "NODE_OPTIONS",
    "NPM_CONFIG_CACHE", "NPM_CONFIG_UPDATE_NOTIFIER",
    # Linux runtime settings Node/Chromium may need inside the container (none of them hold secrets)
    "LD_LIBRARY_PATH", "NODE_PATH", "XDG_RUNTIME_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "PUPPETEER_CACHE_DIR", "PUPPETEER_EXECUTABLE_PATH", "CHROME_PATH",
}
_SECRETISH = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|DATABASE_URL|^PG|^AWS_|^BUCKET|ACCESS_KEY",
                        re.IGNORECASE)
_NO_TELEMETRY = {"DO_NOT_TRACK": "1", "HF_CLI_TELEMETRY_DISABLED": "1", "HYPERFRAMES_NO_TELEMETRY": "1",
                 "NPM_CONFIG_UPDATE_NOTIFIER": "false"}


class UnsafeMedia(ValueError):
    """The upload is not a video we are willing to process."""


def clean_env(base: dict[str, str] | None = None) -> dict[str, str]:
    base = os.environ if base is None else base
    env = {k: v for k, v in base.items() if k.upper() in _PASS and not _SECRETISH.search(k)}
    env.update(_NO_TELEMETRY)
    return env


def sniff_container(path: Path) -> str:
    """'mov' or 'matroska' from the file's leading bytes; raises UnsafeMedia for anything else."""
    with open(path, "rb") as f:
        head = f.read(64)
    if head.startswith(EBML_MAGIC):
        return "matroska"
    if len(head) >= 8 and head[4:8] in MOV_ATOMS:
        return "mov"
    raise UnsafeMedia("not an MP4, MOV, MKV or WebM video (unrecognised file contents)")


def ffmpeg_input(demuxer: str) -> list[str]:
    """Options to put directly before `-i <upload>`. For MOV, external data references (drefs) are switched off
    explicitly (ffmpeg's default is off too) so a crafted file can't point ffmpeg at other local files."""
    return ["-protocol_whitelist", PROTOCOLS, "-f", demuxer, *(["-enable_drefs", "0"] if demuxer == "mov" else [])]


@dataclass
class MediaInfo:
    demuxer: str
    duration: float
    width: int
    height: int
    hdr: str | None = None      # the HDR transfer function (e.g. "arib-std-b67"), None for ordinary video


def check_upload(path: Path, max_seconds: float = 600.0, min_seconds: float = 1.0) -> MediaInfo:
    """Accept only a real single video with audio, of sane length. Raises UnsafeMedia with a plain reason."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise UnsafeMedia("the file is missing or empty")
    demuxer = sniff_container(path)
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", *ffmpeg_input(demuxer), "-show_entries",
             "format=format_name,duration:stream=codec_type,width,height,color_transfer", "-of", "json", str(path)],
            capture_output=True, text=True, env=clean_env(), timeout=120)
    except subprocess.TimeoutExpired as err:
        raise UnsafeMedia("the video took too long to read (it may be damaged)") from err
    if res.returncode != 0:
        raise UnsafeMedia("the video could not be read (it may be damaged)")
    try:
        info = json.loads(res.stdout)
        fmt = info["format"]
        duration = float(fmt["duration"])
    except (ValueError, KeyError, TypeError) as err:
        raise UnsafeMedia("the video could not be read (no duration)") from err
    if fmt.get("format_name") != DEMUXERS[demuxer]:
        raise UnsafeMedia(f"unexpected container type {fmt.get('format_name')!r}")
    streams = info.get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video"]
    if not video:
        raise UnsafeMedia("the file has no video track")
    if not any(s.get("codec_type") == "audio" for s in streams):
        raise UnsafeMedia("the video has no sound track")
    if not min_seconds <= duration <= max_seconds:
        raise UnsafeMedia(f"the video is {duration:.0f} seconds long; the limit is {max_seconds / 60:.0f} minutes")
    for v in video:                       # every video track, not just the one ffmpeg would pick
        w, h = int(v.get("width") or 0), int(v.get("height") or 0)
        if not (0 < w <= MAX_SIDE and 0 < h <= MAX_SIDE):
            raise UnsafeMedia(f"the video size {w}x{h} is not supported (up to 4K)")
    width, height = int(video[0].get("width") or 0), int(video[0].get("height") or 0)
    hdr = next((v.get("color_transfer") for v in video if v.get("color_transfer") in HDR_TRANSFERS), None)
    return MediaInfo(demuxer, duration, width, height, hdr)
