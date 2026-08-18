# The capture engine

Video → durable library capture. Point it at a URL or a local file and it writes the raw material your agent can reread forever: a timestamped transcript, chapter-aware deduplicated frames, source metadata, and a cost receipt. Capture *is* keeping — the output directory is the library's `raw/<slug>/`.

The receipt from a real run — a 59:48 talk, captured with captions:

```
$ python3 capture.py "https://www.youtube.com/watch?v=zjkBMFhNj_g" --transcript-only
▸ probing https://www.youtube.com/watch?v=zjkBMFhNj_g
▸ captions fast path (no download)
✓ captured “[1hr Talk] Intro to Large Language Models” → raw/intro-to-large-language-models
  duration 59:48 · 21 chapters
  transcript: captions · ~16,462 tokens
  cost to read it all: ~16,462 tokens · 4.4s wall · 0.1MB on disk
```

4.4 seconds for the hour because caption-first capture never downloads the video: when the source has captions, the engine pulls them straight down and the whole talk costs one HTTP request. No captions? It falls back to Whisper if you've configured a key (see the exception below), or metadata-plus-frames with nothing leaving your machine.

## Run it

```
python3 capture.py <url-or-path> [options]

--detail glance|standard|fine    frame density + resolution (default: standard)
--transcript-only                skip download + frames (near-free with captions)
--start MM:SS --end MM:SS        capture a window instead of the whole video
--out DIR                        output dir (pass your library's raw/<slug>/)
--no-whisper                     never call a transcription API
--json                           machine-readable receipt on stdout
```

Needs Python 3.9+, stdlib only — no pip installs. `yt-dlp` and `ffmpeg` are checked lazily, per operation: a captions-only capture of a URL needs just `yt-dlp`; a local file with frames needs just `ffmpeg`.

## What you get

```
raw/<slug>/
  source.md            metadata: title, channel, duration, chapters, capture date
  transcript.md        timestamped, chapter-segmented
  frames/              deduplicated stills, timestamped, anchored to chapters
  frames-manifest.md   one line per frame: timestamp, chapter, filename
  receipt.json         tokens, wall time, bytes — what this capture cost
```

Captures are atomic (built in a `.partial` sibling, renamed on success — a failed capture leaves nothing) and immutable: re-capturing an existing `raw/<slug>/` refuses rather than overwrites. Re-lens from the raw instead; that's what it's for.

## The one exception to local-first

If you configure a Whisper API key (`GROQ_API_KEY` or `OPENAI_API_KEY`, in your environment or `~/.config/magpie/.env`), videos *without* captions get their extracted audio uploaded to that provider for transcription — and the engine announces before it uploads. No key → captionless videos still capture (metadata + frames, no transcript) and nothing ever leaves your machine. `--no-whisper` forces fully-local either way.

## Tests

```
python3 tests/test_capture_logic.py
```

29 tests, pure functions only — no network, no ffmpeg.
