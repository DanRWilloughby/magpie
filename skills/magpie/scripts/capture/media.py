"""Media layer for the Magpie capture engine: probing, captions, download,
frame extraction, and perceptual dedup. Everything shells out to yt-dlp /
ffmpeg / ffprobe; pure-Python otherwise (stdlib only).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

FFMPEG_LOGLEVEL = ["-hide_banner", "-loglevel", "error"]

# ---------------------------------------------------------------- deps

REQUIRED_BINARIES = ("yt-dlp", "ffmpeg", "ffprobe")


def missing_deps() -> list[str]:
    return [b for b in REQUIRED_BINARIES if shutil.which(b) is None]


def require(*bins: str) -> None:
    """Check only the binaries this operation actually needs (a captions-only
    remote capture must not fail because ffmpeg is absent)."""
    missing = [b for b in bins if shutil.which(b) is None]
    if missing:
        raise RuntimeError(f"missing: {', '.join(missing)}\n{install_hint()}")


def install_hint() -> str:
    return (
        "Missing tools. Install with:\n"
        "  macOS:  brew install yt-dlp ffmpeg\n"
        "  Linux:  pipx install yt-dlp && sudo apt install ffmpeg\n"
        "  Windows: winget install yt-dlp.yt-dlp Gyan.FFmpeg"
    )


def run(cmd: list[str], timeout: int = 600, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def validate_remote_url(url: str) -> str:
    """Remote sources must be plain http(s) URLs: no other schemes, no
    embedded credentials, nothing that could read as a command-line option."""
    from urllib.parse import urlsplit
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise RuntimeError(f"not a supported URL (need http/https): {url[:120]}")
    if u.username or u.password:
        raise RuntimeError("URLs with embedded credentials are not supported")
    return url


def _host_is_private(hostname: str) -> bool:
    import ipaddress
    import socket
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return True  # unresolvable — refuse rather than guess
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    return False


# ---------------------------------------------------------------- probe

def sanitize_meta(text: str) -> str:
    """External titles/chapter names land in Markdown headings and manifests —
    strip control characters and collapse whitespace so hostile metadata can't
    fake structure (extra headings, fences) or smuggle terminal escapes."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class SourceInfo:
    title: str
    duration_s: float
    is_local: bool
    url: str  # original URL, or absolute path for local files
    channel: str = ""
    upload_date: str = ""  # YYYY-MM-DD or ""
    chapters: list[dict] = field(default_factory=list)  # {title,start_time,end_time}
    has_subtitles: bool = False
    caption_url: str | None = None  # direct VTT URL from the probe (saves a yt-dlp launch)


def probe(source: str) -> SourceInfo:
    p = Path(source).expanduser()
    if p.exists():
        return _probe_local(p)
    return _probe_remote(source)


def _probe_local(path: Path) -> SourceInfo:
    require("ffprobe")
    r = run(["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_chapters", str(path)])
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {r.stderr.strip()[:300]}")
    data = json.loads(r.stdout or "{}")
    fmt = data.get("format", {})
    chapters = [
        {
            "title": sanitize_meta((c.get("tags") or {}).get("title", f"Chapter {i + 1}")),
            "start_time": float(c.get("start_time", 0)),
            "end_time": float(c.get("end_time", 0)),
        }
        for i, c in enumerate(data.get("chapters", []))
    ]
    return SourceInfo(
        title=sanitize_meta((fmt.get("tags") or {}).get("title") or path.stem),
        duration_s=float(fmt.get("duration", 0)),
        is_local=True,
        url=str(path.resolve()),
        chapters=chapters,
    )


def _probe_remote(url: str) -> SourceInfo:
    require("yt-dlp")
    validate_remote_url(url)
    r = run(["yt-dlp", "-J", "--no-playlist", "--", url], timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp probe failed: {r.stderr.strip()[:300]}")
    data = json.loads(r.stdout)
    upload = data.get("upload_date") or ""
    if len(upload) == 8:
        upload = f"{upload[:4]}-{upload[4:6]}-{upload[6:]}"
    chapters = [
        {
            "title": sanitize_meta(c.get("title", f"Chapter {i + 1}")),
            "start_time": float(c.get("start_time", 0)),
            "end_time": float(c.get("end_time", 0)),
        }
        for i, c in enumerate(data.get("chapters") or [])
    ]
    subs = bool(data.get("subtitles")) or bool(data.get("automatic_captions"))
    return SourceInfo(
        title=sanitize_meta(data.get("title") or "capture"),
        duration_s=float(data.get("duration") or 0),
        is_local=False,
        url=data.get("webpage_url") or url,
        channel=sanitize_meta(data.get("channel") or data.get("uploader") or ""),
        upload_date=upload,
        chapters=chapters,
        has_subtitles=subs,
        caption_url=pick_caption_track(data.get("subtitles") or {},
                                       data.get("automatic_captions") or {}),
    )


def pick_caption_track(subtitles: dict, auto_captions: dict) -> str | None:
    """Choose an English VTT caption URL from probe metadata — manual subs
    beat auto-captions. Only vtt qualifies (it's what our parser reads);
    no English vtt track → None, and the caller falls back to yt-dlp."""
    for tracks in (subtitles, auto_captions):
        for lang, fmts in tracks.items():
            if lang == "en" or lang.startswith("en-"):
                for f in fmts:
                    if f.get("ext") == "vtt" and f.get("url"):
                        return f["url"]
    return None


# ---------------------------------------------------------------- captions

def fetch_caption_text(caption_url: str) -> str | None:
    """Direct VTT download from a probe-supplied track URL — one HTTP GET,
    no second yt-dlp process. Returns None on any failure (caller falls back).
    The URL comes from source metadata (untrusted): https only, no embedded
    credentials, and never a private/loopback/link-local destination."""
    import urllib.request
    from urllib.parse import urlsplit
    try:
        u = urlsplit(caption_url)
        if u.scheme != "https" or not u.hostname or u.username or u.password:
            return None
        if _host_is_private(u.hostname):
            return None
        req = urllib.request.Request(caption_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read(20 * 1024 * 1024 + 1)  # captions cap: 20MB is absurdly generous
            if len(raw) > 20 * 1024 * 1024:
                return None
            text = raw.decode("utf-8", errors="replace")
        return text if text.lstrip().startswith("WEBVTT") else None
    except OSError:
        return None


def fetch_captions(url: str, workdir: Path) -> Path | None:
    """Caption-first fast path: pull subtitles WITHOUT downloading the video.
    Returns the .vtt path, or None if the source has no captions."""
    workdir.mkdir(parents=True, exist_ok=True)
    require("yt-dlp")
    validate_remote_url(url)
    r = run([
        "yt-dlp", "--skip-download", "--no-playlist",
        "--write-subs", "--write-auto-subs",
        "--sub-langs", "en.*,en,-live_chat",
        "--sub-format", "vtt",
        "-o", str(workdir / "captions.%(ext)s"),
        "--", url,
    ], timeout=180)
    if r.returncode != 0:
        return None
    vtts = sorted(workdir.glob("captions.*.vtt"))
    return vtts[0] if vtts else None


# ---------------------------------------------------------------- download

def download_video(url: str, workdir: Path, max_height: int = 720) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    require("yt-dlp", "ffmpeg")  # ffmpeg merges the bv+ba streams
    validate_remote_url(url)
    out = workdir / "video.%(ext)s"
    r = run([
        "yt-dlp", "--no-playlist", "--max-filesize", "4G",
        "-f", f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
        "--merge-output-format", "mp4",
        "-o", str(out),
        "--", url,
    ], timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp download failed: {r.stderr.strip()[:300]}")
    vids = [p for p in workdir.glob("video.*") if p.suffix in (".mp4", ".mkv", ".webm", ".mov")]
    if not vids:
        raise RuntimeError("download produced no video file")
    return vids[0]


def extract_audio(video: Path, workdir: Path, start: float | None = None,
                  end: float | None = None) -> Path:
    """Mono 48kbps mp3 — small enough for hour-scale Whisper uploads."""
    out = workdir / "audio.mp3"
    require("ffmpeg")
    cmd = ["ffmpeg", *FFMPEG_LOGLEVEL, "-y"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(video)]
    if end:
        cmd += ["-to", str(max(0.0, end - (start or 0)))]
    cmd += ["-vn", "-ac", "1", "-b:a", "48k", str(out)]
    r = run(cmd, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"audio extract failed: {r.stderr.strip()[:300]}")
    return out


# ---------------------------------------------------------------- frames

DETAIL_MODES = {
    # mode: (floor, cap, per_minute, base, width)
    "glance": (6, 16, 0.5, 4, 640),
    "standard": (10, 32, 0.8, 6, 960),
    "fine": (16, 64, 1.5, 8, 1280),
}


def frame_budget(duration_s: float, detail: str) -> int:
    lo, hi, k, c, _w = DETAIL_MODES[detail]
    return int(min(hi, max(lo, round((duration_s / 60) * k) + c)))


def frame_width(detail: str) -> int:
    return DETAIL_MODES[detail][4]


@dataclass
class Candidate:
    t: float
    protected: bool = False  # chapter starts always survive dedup
    chapter: str = ""


OVERSAMPLE = 2.5
EDGE_PAD_FRAC = 0.02  # skip intro/outro cards at the extreme edges


def allocate_candidates(duration_s: float, budget: int, chapters: list[dict],
                        start: float | None = None, end: float | None = None) -> list[Candidate]:
    """Chapter-aware oversampling: every chapter start is a protected candidate;
    the rest are uniform across the (windowed) duration. Dedup trims to budget."""
    lo = start if start is not None else duration_s * EDGE_PAD_FRAC
    hi = end if end is not None else duration_s * (1 - EDGE_PAD_FRAC)
    hi = max(hi, lo + 1)

    def chapter_at(t: float) -> str:
        for ch in chapters:
            if ch["start_time"] <= t < (ch["end_time"] or duration_s):
                return ch["title"]
        return ""

    # Chapter starts are author-marked anchors: honor the user window but not
    # the edge pad (a chapter legitimately starts at t=0).
    user_lo = start if start is not None else 0.0
    user_hi = end if end is not None else duration_s
    cands: list[Candidate] = []
    for ch in chapters:
        t = ch["start_time"] + 1.0  # +1s: past the cut/title card
        if user_lo <= t <= user_hi:
            cands.append(Candidate(t=t, protected=True, chapter=ch["title"]))

    n = max(int(budget * OVERSAMPLE), budget + 2)
    span = hi - lo
    for i in range(n):
        t = lo + span * (i + 0.5) / n
        cands.append(Candidate(t=t, chapter=chapter_at(t)))

    cands.sort(key=lambda c: c.t)
    return cands


def extract_frame(video: Path, t: float, out_jpg: Path, width: int) -> None:
    require("ffmpeg")
    r = run([
        "ffmpeg", *FFMPEG_LOGLEVEL, "-y",
        "-ss", f"{t:.2f}", "-i", str(video),
        "-frames:v", "1", "-vf", f"scale={width}:-2",
        "-q:v", "3", str(out_jpg),
    ], timeout=60)
    if r.returncode != 0 or not out_jpg.exists():
        raise RuntimeError(f"frame extract failed at t={t:.1f}: {r.stderr.strip()[:200]}")


# ---------------------------------------------------------------- dedup (aHash)

def ahash_of_jpg(jpg: Path) -> int:
    """Classic average-hash: 8x8 grayscale, threshold at the mean → 64-bit hash.
    ffmpeg emits the 64 raw gray bytes; no image library needed."""
    r = subprocess.run(
        ["ffmpeg", *FFMPEG_LOGLEVEL, "-i", str(jpg),
         "-vf", "scale=8:8", "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1"],
        capture_output=True, timeout=30,
    )
    px = r.stdout
    if len(px) < 64:
        return 0
    px = px[:64]
    mean = sum(px) / 64
    h = 0
    for b in px:
        h = (h << 1) | (1 if b > mean else 0)
    return h


def hamming(a: int, b: int) -> int:
    # bin().count keeps us compatible with Python 3.9 (int.bit_count is 3.10+)
    return bin(a ^ b).count("1")


DEDUP_THRESHOLD = 6  # Hamming distance at or under this = same shot


def dedup_frames(hashes: list[tuple[int, bool]]) -> list[bool]:
    """Given (ahash, protected) per candidate in time order, return keep-flags.
    A frame is dropped when it's within DEDUP_THRESHOLD of any already-kept
    frame — unless protected (chapter starts always survive)."""
    kept: list[int] = []
    keep_flags: list[bool] = []
    for h, protected in hashes:
        dup = any(hamming(h, k) <= DEDUP_THRESHOLD for k in kept)
        if protected or not dup:
            kept.append(h)
            keep_flags.append(True)
        else:
            keep_flags.append(False)
    return keep_flags
