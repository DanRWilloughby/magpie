#!/usr/bin/env bash
# magpie doctor — four-state health audit.
# States: WORKING · UNVERIFIED (configured, no run evidence) · NOT WORKING · COULD BE ON (optional, unconfigured)
# Report-only: always exits 0.

LIB="${MAGPIE_LIBRARY:-$HOME/Documents/Magpie}"
VOICE="${MAGPIE_VOICE:-$LIB/voice.md}"

ok()    { printf '  [WORKING]      %s\n' "$1"; }
unv()   { printf '  [UNVERIFIED]   %s\n' "$1"; }
bad()   { printf '  [NOT WORKING]  %s — fix: %s\n' "$1" "$2"; }
could() { printf '  [COULD BE ON]  %s — %s\n' "$1" "$2"; }

echo "magpie doctor — $(date +%Y-%m-%d)"
echo
echo "Core dependencies"
command -v yt-dlp >/dev/null 2>&1 && ok "yt-dlp ($(yt-dlp --version 2>/dev/null))" || bad "yt-dlp (video capture)" "brew install yt-dlp  (or pipx install yt-dlp)"
command -v ffmpeg >/dev/null 2>&1 && ok "ffmpeg (frame extraction)" || could "ffmpeg" "brew install ffmpeg — enables frame capture for demo/tutorial videos"
command -v gh >/dev/null 2>&1 && { gh auth status >/dev/null 2>&1 && ok "gh CLI authed (richer GitHub captures)" || unv "gh CLI installed, auth unverified — run: gh auth status"; } || could "gh CLI" "brew install gh — richer GitHub captures for the rival lens"
if [ -n "${GROQ_API_KEY:-}" ] || [ -n "${OPENAI_API_KEY:-}" ] || grep -qsE "^(GROQ|OPENAI)_API_KEY=.+" "$HOME/.config/magpie/.env" 2>/dev/null; then ok "whisper key (captionless-video transcription)"; else could "whisper key" "set GROQ_API_KEY or OPENAI_API_KEY (env or ~/.config/magpie/.env) — enables transcripts for videos without captions"; fi

echo
echo "Library"
if [ -d "$LIB" ]; then
  ok "library root: $LIB"
  for d in raw notes rivals digests; do
    [ -d "$LIB/$d" ] && ok "$d/" || could "$d/" "created on first use"
  done
  for f in index.md queue.md log.md CLAUDE.md; do
    [ -f "$LIB/$f" ] && ok "$f" || bad "$f" "missing — re-run install.sh or recreate from LIBRARY.md"
  done
  QN=$(grep -c '^\s*[-*]' "$LIB/queue.md" 2>/dev/null); QN=${QN:-0}
  echo "  [INFO]         queue: ${QN} item(s) pending"
  LAST=$(tail -1 "$LIB/log.md" 2>/dev/null | grep -oE '20[0-9]{2}-[0-9]{2}-[0-9]{2}' | head -1)
  [ -n "$LAST" ] && echo "  [INFO]         last capture logged: ${LAST}"
  TRASH_N=$(find "$LIB/.trash" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
  [ "${TRASH_N:-0}" -gt 0 ] && echo "  [INFO]         .trash: ${TRASH_N} quarantined item(s) — purge those older than 14 days"
else
  bad "library root: $LIB" "run install.sh, or set MAGPIE_LIBRARY to your library path"
fi

echo
echo "Optional capability"
if [ -f "$LIB/profile.md" ]; then
  ok "profile.md (owner context — powers rival routing, digests, respond)"
else
  could "profile.md" "run /magpie start — one question, doubles as your first capture"
fi
if [ -f "$VOICE" ]; then
  if grep -q '<!--' "$VOICE" 2>/dev/null; then
    unv "voice file exists but still has template comments — fill it in: $VOICE"
  else
    ok "voice file for --respond: $VOICE"
  fi
else
  could "voice file for --respond" "copy assets/voice-template.md to $VOICE and fill it in"
fi
could "captionless-video transcripts" "not in this preview — videos without captions get metadata-only capture"

echo
echo "Done. Report-only; nothing was changed."
exit 0
