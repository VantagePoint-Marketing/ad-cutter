"""Hostile-upload and environment-scrubbing tests for safe_media."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import safe_media as sm  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg/ffprobe not installed")


def make_video(path: Path, seconds: float = 2.0, audio: bool = True, fmt_args: tuple = ()) -> Path:
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=160x284:rate=10:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += [*fmt_args, "-shortest", str(path)]
    subprocess.run(cmd, check=True)
    return path


# ---------------------------------------------------------------- environment

def test_clean_env_drops_secrets_and_keeps_basics():
    base = {"PATH": "/usr/bin", "HOME": "/home/app", "OPENROUTER_API_KEY": "sk-or-x", "GEMINI_API_KEY": "g",
            "DATABASE_URL": "postgres://u:p@h/db", "AWS_SECRET_ACCESS_KEY": "s", "BUCKET_SECRET": "b",
            "PGPASSWORD": "p", "FOREPLAY_API_KEY": "f", "YOUTUBE_API_KEY": "y", "RANDOM_THING": "r",
            "HYPERFRAMES_BROWSER_PATH": "/opt/chrome-offline"}
    env = sm.clean_env(base)
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/app"
    assert env["HYPERFRAMES_BROWSER_PATH"] == "/opt/chrome-offline"
    for leaked in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "DATABASE_URL", "AWS_SECRET_ACCESS_KEY", "BUCKET_SECRET",
                   "PGPASSWORD", "FOREPLAY_API_KEY", "YOUTUBE_API_KEY", "RANDOM_THING"):
        assert leaked not in env
    assert env["DO_NOT_TRACK"] == "1" and env["HYPERFRAMES_NO_TELEMETRY"] == "1"


def test_clean_env_never_passes_a_secret_looking_name_even_if_allowlisted(monkeypatch):
    monkeypatch.setattr(sm, "_PASS", sm._PASS | {"NODE_AUTH_TOKEN"})
    assert "NODE_AUTH_TOKEN" not in sm.clean_env({"NODE_AUTH_TOKEN": "t", "PATH": "/bin"})


# ---------------------------------------------------------------- container sniffing (no ffmpeg needed)

@pytest.mark.parametrize("content", [
    b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nfile:///proc/self/environ\n#EXT-X-ENDLIST\n",   # HLS playlist
    b"ffconcat version 1.0\nfile '/proc/self/environ'\n",                                              # concat list
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,                                                                 # image
    b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",                                                   # text
    b"",                                                                                                  # empty
])
def test_sniff_rejects_non_video_disguised_as_mp4(tmp_path, content):
    f = tmp_path / "video.mp4"
    f.write_bytes(content)
    with pytest.raises(sm.UnsafeMedia):
        sm.check_upload(f)


def test_sniff_recognises_mov_and_matroska_headers(tmp_path):
    mov = tmp_path / "a.bin"
    mov.write_bytes(b"\x00\x00\x00\x18ftypqt  " + b"\x00" * 32)
    mkv = tmp_path / "b.bin"
    mkv.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 32)
    assert sm.sniff_container(mov) == "mov"
    assert sm.sniff_container(mkv) == "matroska"


def test_ffmpeg_input_forces_demuxer_whitelist_and_no_drefs():
    assert sm.ffmpeg_input("mov") == ["-protocol_whitelist", "file", "-f", "mov", "-enable_drefs", "0"]
    assert sm.ffmpeg_input("matroska") == ["-protocol_whitelist", "file", "-f", "matroska"]


@pytest.mark.parametrize("atom", [b"styp", b"sidx", b"junk", b"uuid", b"wide", b"mdat"])
def test_sniff_accepts_other_legitimate_mov_first_atoms(tmp_path, atom):
    f = tmp_path / "v.mov"
    f.write_bytes(b"\x00\x00\x00\x10" + atom + b"\x00" * 32)
    assert sm.sniff_container(f) == "mov"


@needs_ffmpeg
def test_rejects_oversized_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "MAX_SIDE", 100)            # the fixture is 160x284
    with pytest.raises(sm.UnsafeMedia, match="size"):
        sm.check_upload(make_video(tmp_path / "big.mp4"))


def test_probe_timeout_becomes_a_clean_rejection(tmp_path, monkeypatch):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32)

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=120)
    monkeypatch.setattr(sm.subprocess, "run", slow)
    with pytest.raises(sm.UnsafeMedia, match="too long to read"):
        sm.check_upload(f)


# ---------------------------------------------------------------- real probing

@needs_ffmpeg
def test_accepts_real_mp4_with_audio(tmp_path):
    info = sm.check_upload(make_video(tmp_path / "ok.mp4"))
    assert info.demuxer == "mov" and 1.5 < info.duration < 2.5 and info.width == 160


@needs_ffmpeg
def test_accepts_webm_named_mp4(tmp_path):
    f = make_video(tmp_path / "clip.mp4", fmt_args=("-c:v", "libvpx-vp9", "-c:a", "libopus", "-f", "webm"))
    assert sm.check_upload(f).demuxer == "matroska"


@needs_ffmpeg
def test_rejects_video_without_sound(tmp_path):
    with pytest.raises(sm.UnsafeMedia, match="no sound"):
        sm.check_upload(make_video(tmp_path / "silent.mp4", audio=False))


@needs_ffmpeg
def test_rejects_too_long(tmp_path):
    with pytest.raises(sm.UnsafeMedia, match="limit"):
        sm.check_upload(make_video(tmp_path / "long.mp4", seconds=3), max_seconds=2)


@needs_ffmpeg
def test_rejects_audio_only_mp4(tmp_path):
    f = tmp_path / "audio.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=2", str(f)], check=True)
    with pytest.raises(sm.UnsafeMedia, match="no video"):
        sm.check_upload(f)


@needs_ffmpeg
def test_rejects_truncated_mp4(tmp_path):
    good = make_video(tmp_path / "good.mp4")
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(good.read_bytes()[:200])      # keeps the ftyp header, loses the rest
    with pytest.raises(sm.UnsafeMedia):
        sm.check_upload(bad)


# ---------------------------------------------------------------- HDR video (iPhone HDR is HLG)

def has_encoder(name: str) -> bool:
    res = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return name in res.stdout


def make_hlg(path: Path, seconds: float = 2.0) -> Path:
    """A 10-bit HLG clip tagged the way an iPhone writes HDR video."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=320x568:rate=30:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-vf", "format=yuv420p10le",
                    "-c:v", "libx265", "-x265-params",
                    "colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc:range=limited:log-level=error",
                    "-tag:v", "hvc1", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


needs_hlg = pytest.mark.skipif(not (shutil.which("ffmpeg") and has_encoder("libx265")), reason="needs ffmpeg with libx265")


@needs_hlg
def test_hdr_video_is_recognised_and_ordinary_video_is_not(tmp_path):
    assert sm.check_upload(make_hlg(tmp_path / "hlg.mp4")).hdr == "arib-std-b67"
    assert sm.check_upload(make_video(tmp_path / "ok.mp4")).hdr is None
