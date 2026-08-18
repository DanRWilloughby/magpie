"""Transcript layer for the Magpie capture engine: VTT parsing/cleanup and the
optional Whisper API fallback for captionless sources. Stdlib only."""
from __future__ import annotations

import json
import os
import re
import urllib.request
import uuid
from pathlib import Path

# ---------------------------------------------------------------- timestamps

def parse_ts(s: str) -> float:
    """'45' | '12:34' | '1:02:03' | '00:01:02.500' → seconds."""
    parts = s.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"bad timestamp: {s!r}")
    sec = 0.0
    for p in parts:
        sec = sec * 60 + float(p)
    return sec


def fmt_ts(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


# ---------------------------------------------------------------- VTT

CUE_RE = re.compile(r"(\d{1,2}:\d{2}:\d{2}\.\d{3})\s+-->\s+(\d{1,2}:\d{2}:\d{2}\.\d{3})")
TAG_RE = re.compile(r"<[^>]+>")


def parse_vtt(text: str) -> list[dict]:
    """VTT → [{start: seconds, text: str}], cleaned: styling tags stripped,
    auto-caption rolling duplicates collapsed. YouTube auto-subs repeat each
    line across consecutive cues (cue N shows lines A+B, cue N+1 shows B+C),
    so a line already present in the previous cue is dropped."""
    raw_cues: list[tuple[float, list[str]]] = []
    cur_start: float | None = None
    cur_lines: list[str] = []

    def flush():
        nonlocal cur_start, cur_lines
        if cur_start is not None and cur_lines:
            raw_cues.append((cur_start, cur_lines))
        cur_start, cur_lines = None, []

    for raw in text.splitlines():
        line = raw.strip()
        m = CUE_RE.search(line)
        if m:
            flush()
            cur_start = parse_ts(m.group(1))
            continue
        if cur_start is None or not line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        clean = TAG_RE.sub("", line).strip()
        if clean:
            cur_lines.append(clean)
    flush()

    out: list[dict] = []
    prev: set[str] = set()
    for start, lines in raw_cues:
        fresh = [ln for ln in lines if ln not in prev]
        prev = set(lines)
        if fresh:
            out.append({"start": start, "text": " ".join(fresh)})
    return out


def cues_to_markdown(cues: list[dict], group_s: int = 30) -> str:
    """Group cues into ~group_s windows: `**[M:SS]** text…` per block."""
    if not cues:
        return ""
    blocks: list[str] = []
    win_start = cues[0]["start"]
    buf: list[str] = []
    for cue in cues:
        if cue["start"] - win_start >= group_s and buf:
            blocks.append(f"**[{fmt_ts(win_start)}]** " + " ".join(buf))
            win_start, buf = cue["start"], []
        buf.append(cue["text"])
    if buf:
        blocks.append(f"**[{fmt_ts(win_start)}]** " + " ".join(buf))
    return "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------- Whisper fallback

def find_whisper_key() -> tuple[str, str, str] | None:
    """Locate a Whisper-capable API key. Returns (provider, endpoint, key) or None.
    Order: env vars, then ~/.config/magpie/.env. Magpie only uses credentials
    explicitly supplied through its own environment or its own config file —
    it never reads another application's configuration."""
    sources: list[dict[str, str]] = [dict(os.environ)]
    for cfg in (Path.home() / ".config/magpie/.env",):
        if cfg.exists():
            d: dict[str, str] = {}
            for line in cfg.read_text().splitlines():
                m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
                if m:
                    d[m.group(1)] = m.group(2).strip().strip('"')
            sources.append(d)
    for src in sources:
        if src.get("GROQ_API_KEY"):
            return ("groq", "https://api.groq.com/openai/v1/audio/transcriptions",
                    src["GROQ_API_KEY"])
        if src.get("OPENAI_API_KEY"):
            return ("openai", "https://api.openai.com/v1/audio/transcriptions",
                    src["OPENAI_API_KEY"])
    return None


WHISPER_MODELS = {"groq": "whisper-large-v3", "openai": "whisper-1"}


def whisper_transcribe(audio: Path, provider: str, endpoint: str, key: str,
                       t_offset: float = 0.0) -> list[dict]:
    """Transcribe one audio file via an OpenAI-compatible Whisper endpoint.
    Returns cues [{start, text}] with t_offset added (for windowed captures)."""
    boundary = uuid.uuid4().hex
    payload = audio.read_bytes()
    if len(payload) > 24 * 1024 * 1024:
        raise RuntimeError(
            f"audio is {len(payload) / 1e6:.0f}MB (>24MB API limit) — "
            "re-run with --start/--end windows"
        )

    def part(name: str, value: str) -> bytes:
        return (f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="{name}"\r\n\r\n{value}\r\n').encode()

    body = part("model", WHISPER_MODELS[provider])
    body += part("response_format", "verbose_json")
    body += (f"--{boundary}\r\nContent-Disposition: form-data; "
             f'name="file"; filename="audio.mp3"\r\n'
             f"Content-Type: audio/mpeg\r\n\r\n").encode()
    body += payload + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read())
    return [
        {"start": float(seg.get("start", 0)) + t_offset, "text": seg.get("text", "").strip()}
        for seg in data.get("segments", [])
        if seg.get("text", "").strip()
    ]
