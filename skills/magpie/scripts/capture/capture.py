#!/usr/bin/env python3
"""Magpie capture engine — video → durable library capture.

Turns any video (URL via yt-dlp, or a local file) into library-native raw
capture: a timestamped transcript, chapter-aware deduplicated frames, a frames
manifest, source metadata, and a cost receipt. Nothing is thrown away: the
output directory IS the library's raw/<slug>/ — capture is keeping.

Usage:
  python3 capture.py <url-or-path> [options]

Options:
  --detail glance|standard|fine   Frame density + resolution (default: standard)
  --transcript-only               Skip download + frames entirely (near-free
                                  when the source has captions)
  --start MM:SS --end MM:SS       Capture a window instead of the whole video
  --out DIR                       Output dir (default: ./magpie-capture/<slug>)
                                  Pass the library's raw/<slug>/ to write in place
  --slug NAME                     Override the derived slug
  --no-whisper                    Never call a transcription API
  --json                          Machine-readable receipt on stdout
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import media  # noqa: E402
import transcribe  # noqa: E402
from transcribe import cues_to_markdown, fmt_ts, parse_ts, parse_vtt  # noqa: E402

# ---------------------------------------------------------------- receipt math

VISION_TOKENS_PER_PX = 1 / 750  # Claude vision: ~ (w*h)/750 tokens per image
CHARS_PER_TOKEN = 4


def estimate_transcript_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def estimate_frame_tokens(dims: list[tuple[int, int]]) -> int:
    return int(sum(w * h for w, h in dims) * VISION_TOKENS_PER_PX)


def slugify(title: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    s = s[:max_len].rstrip("-")
    return s or "capture"


# ---------------------------------------------------------------- pipeline

def capture(source: str, detail: str = "standard", transcript_only: bool = False,
            start: float | None = None, end: float | None = None,
            out_dir: Path | None = None, slug: str | None = None,
            allow_whisper: bool = True, quiet: bool = False) -> dict:
    try:
        return _capture(source, detail=detail, transcript_only=transcript_only,
                        start=start, end=end, out_dir=out_dir, slug=slug,
                        allow_whisper=allow_whisper, quiet=quiet)
    finally:
        # never leave a hidden partial behind, success or failure
        base = out_dir or (Path.cwd() / "magpie-capture")
        parent = base.parent if out_dir else base
        if parent.exists():
            for p in parent.glob(".*.partial"):
                shutil.rmtree(p, ignore_errors=True)


def _capture(source: str, detail: str = "standard", transcript_only: bool = False,
             start: float | None = None, end: float | None = None,
             out_dir: Path | None = None, slug: str | None = None,
             allow_whisper: bool = True, quiet: bool = False) -> dict:
    t0 = time.time()
    log = (lambda *a: None) if quiet else (lambda *a: print(*a, file=sys.stderr))

    log(f"▸ probing {source}")
    info = media.probe(source)
    slug = slug or slugify(info.title)
    final_out = out_dir or (Path.cwd() / "magpie-capture" / slug)
    if final_out.exists() and any(final_out.iterdir()):
        raise RuntimeError(
            f"{final_out} already contains a capture — raw captures are immutable. "
            "Use --slug/--out for a new capture, or remove the directory deliberately.")
    # Build in a hidden sibling and rename into place at the end, so an
    # interrupted capture never leaves a partial dir that looks like a real one.
    out = final_out.parent / f".{final_out.name}.partial"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    frames_dir = out / "frames"

    receipt: dict = {
        "source": info.url, "title": info.title, "slug": slug,
        "duration_s": round(info.duration_s), "detail": detail,
        "chapters": len(info.chapters), "window": None,
        "transcript": {"method": "none", "chars": 0, "est_tokens": 0},
        "frames": {"kept": 0, "candidates": 0, "dropped_as_dupes": 0,
                   "width": 0, "est_tokens": 0},
    }
    if start is not None or end is not None:
        receipt["window"] = f"{fmt_ts(start or 0)}–{fmt_ts(end or info.duration_s)}"

    with tempfile.TemporaryDirectory(prefix="magpie-cap-") as tmp:
        work = Path(tmp)

        # ---- transcript: captions first (no download), whisper fallback
        cues: list[dict] = []
        if not info.is_local:
            log("▸ captions fast path (no download)")
            vtt_text: str | None = None
            if info.caption_url:  # direct GET — the probe already found the track
                vtt_text = media.fetch_caption_text(info.caption_url)
            if vtt_text is None:  # no track in probe, or direct GET failed
                vtt = media.fetch_captions(info.url, work / "subs")
                if vtt:
                    vtt_text = vtt.read_text(errors="replace")
            if vtt_text:
                cues = parse_vtt(vtt_text)
                receipt["transcript"]["method"] = "captions"

        video: Path | None = Path(info.url) if info.is_local else None
        need_video = not transcript_only or (not cues and allow_whisper)
        if not info.is_local and need_video:
            log("▸ downloading (≤720p)")
            video = media.download_video(info.url, work / "dl")

        if not cues and allow_whisper and video is not None:
            key = transcribe.find_whisper_key()
            if key:
                provider, endpoint, k = key
                log(f"▸ no captions — uploading extracted audio to {provider} for "
                    f"transcription (whisper key configured; --no-whisper to keep "
                    f"this capture fully local)")
                audio = media.extract_audio(video, work, start=start, end=end)
                cues = transcribe.whisper_transcribe(audio, provider, endpoint, k,
                                                     t_offset=start or 0.0)
                receipt["transcript"]["method"] = f"whisper/{provider}"
            else:
                log("▸ no captions and no whisper key — transcript skipped")

        if start is not None or end is not None:
            lo, hi = start or 0.0, end or float("inf")
            cues = [c for c in cues if lo <= c["start"] <= hi]

        transcript_md = cues_to_markdown(cues)
        receipt["transcript"]["chars"] = len(transcript_md)
        receipt["transcript"]["est_tokens"] = estimate_transcript_tokens(transcript_md)

        # ---- frames: chapter-aware oversample → aHash dedup → budget
        manifest_lines: list[str] = []
        if not transcript_only and video is not None and info.duration_s > 0:
            budget = media.frame_budget(info.duration_s, detail)
            width = media.frame_width(detail)
            cands = media.allocate_candidates(info.duration_s, budget,
                                              info.chapters, start=start, end=end)
            log(f"▸ frames: {len(cands)} candidates → budget {budget} @ {width}px")
            frames_dir.mkdir(exist_ok=True)

            extracted: list[tuple[media.Candidate, Path, int]] = []
            for i, c in enumerate(cands):
                jpg = work / f"cand-{i:03d}.jpg"
                try:
                    media.extract_frame(video, c.t, jpg, width)
                except RuntimeError:
                    continue
                extracted.append((c, jpg, media.ahash_of_jpg(jpg)))

            keep = media.dedup_frames([(h, c.protected) for c, _p, h in extracted])
            kept = [(c, p) for (c, p, _h), k in zip(extracted, keep) if k]
            # over budget after dedup → thin uniformly, protecting chapter starts
            while len(kept) > budget:
                idx = next((i for i, (c, _p) in enumerate(kept) if not c.protected), None)
                if idx is None:
                    break
                # drop the unprotected frame closest in time to its neighbor
                gaps = [
                    (kept[i][0].t - kept[i - 1][0].t, i)
                    for i in range(1, len(kept)) if not kept[i][0].protected
                ]
                drop = min(gaps)[1] if gaps else idx
                kept.pop(drop)

            dims: list[tuple[int, int]] = []
            for n, (c, p) in enumerate(kept, 1):
                name = f"frame-{n:03d}-t{fmt_ts(c.t).replace(':', 'm')}s.jpg"
                shutil.copy2(p, frames_dir / name)
                dims.append(_jpg_dims(frames_dir / name) or (width, int(width * 9 / 16)))
                chap = f" — {c.chapter}" if c.chapter else ""
                star = " ⭑chapter-start" if c.protected else ""
                manifest_lines.append(f"- t={fmt_ts(c.t)} `frames/{name}`{chap}{star}")

            receipt["frames"] = {
                "kept": len(kept), "candidates": len(extracted),
                "dropped_as_dupes": len(extracted) - sum(keep),
                "width": width, "est_tokens": estimate_frame_tokens(dims),
            }

    # ---- library-native writes (raw contract: transcript / frames-manifest / source)
    if transcript_md:
        (out / "transcript.md").write_text(
            f"# Transcript — {info.title}\n\n"
            f"*method: {receipt['transcript']['method']} · duration {fmt_ts(info.duration_s)}*\n\n"
            + transcript_md)
    if manifest_lines:
        (out / "frames-manifest.md").write_text(
            f"# Frames — {info.title}\n\n"
            f"*{receipt['frames']['kept']} frames · {receipt['frames']['width']}px · "
            f"detail={detail} · Read each path to view*\n\n"
            + "\n".join(manifest_lines) + "\n")

    src_lines = [f"# Source\n", f"- url: {info.url}", f"- title: {info.title}"]
    if info.channel:
        src_lines.append(f"- channel: {info.channel}")
    if info.upload_date:
        src_lines.append(f"- published: {info.upload_date}")
    src_lines += [
        f"- duration: {fmt_ts(info.duration_s)}",
        f"- fetched: {time.strftime('%Y-%m-%d')}",
        f"- captured-by: magpie capture engine (detail={detail})",
    ]
    if info.chapters:
        src_lines.append("- chapters:")
        src_lines += [f"  - [{fmt_ts(c['start_time'])}] {c['title']}" for c in info.chapters]
    (out / "source.md").write_text("\n".join(src_lines) + "\n")

    receipt["est_total_tokens"] = (receipt["transcript"]["est_tokens"]
                                   + receipt["frames"]["est_tokens"])
    receipt["wall_s"] = round(time.time() - t0, 1)
    receipt["bytes_written"] = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    receipt["out_dir"] = str(final_out)
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    final_out.parent.mkdir(parents=True, exist_ok=True)
    if final_out.exists():  # existed-but-empty case from the guard above
        final_out.rmdir()
    out.rename(final_out)
    return receipt


def _jpg_dims(jpg: Path) -> tuple[int, int] | None:
    """JPEG dimensions from the SOF marker — stdlib only."""
    data = jpg.read_bytes()
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h)
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        i += 2 + seg_len
    return None


# ---------------------------------------------------------------- CLI

def human_receipt(r: dict) -> str:
    t, f = r["transcript"], r["frames"]
    lines = [
        f"✓ captured “{r['title']}” → {r['out_dir']}",
        f"  duration {fmt_ts(r['duration_s'])}"
        + (f" · window {r['window']}" if r["window"] else "")
        + (f" · {r['chapters']} chapters" if r["chapters"] else ""),
        f"  transcript: {t['method']} · ~{t['est_tokens']:,} tokens",
    ]
    if f["kept"]:
        lines.append(
            f"  frames: {f['kept']} kept / {f['candidates']} candidates "
            f"({f['dropped_as_dupes']} dupes dropped) · {f['width']}px "
            f"· ~{f['est_tokens']:,} tokens")
    lines.append(
        f"  cost to read it all: ~{r['est_total_tokens']:,} tokens "
        f"· {r['wall_s']}s wall · {r['bytes_written'] / 1e6:.1f}MB on disk")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("--detail", choices=list(media.DETAIL_MODES), default="standard")
    ap.add_argument("--transcript-only", action="store_true")
    ap.add_argument("--start", type=parse_ts, default=None)
    ap.add_argument("--end", type=parse_ts, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--slug", default=None)
    ap.add_argument("--no-whisper", action="store_true")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args(argv)

    try:
        r = capture(args.source, detail=args.detail,
                    transcript_only=args.transcript_only,
                    start=args.start, end=args.end, out_dir=args.out,
                    slug=args.slug, allow_whisper=not args.no_whisper,
                    quiet=args.as_json)
    except (RuntimeError, ValueError) as e:
        print(f"capture failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps(r, indent=2) if args.as_json else human_receipt(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
