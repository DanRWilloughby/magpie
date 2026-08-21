#!/usr/bin/env bash
# Magpie installer. Idempotent. Your library DATA (notes, raw, queue,
# log, index, profile, voice) is never overwritten; LIBRARY.md is app documentation
# and is refreshed on every install.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
LIB="${MAGPIE_LIBRARY:-$HOME/Documents/Magpie}"

echo "Magpie installer"
echo "  skill  → ~/.claude/skills/magpie"
echo "  library→ $LIB"
echo

# 1. Skill (replaced on reinstall — the skill is code, the library is data).
#    Copy → validate → swap, so a failed copy never leaves you without a working install.
mkdir -p "$HOME/.claude/skills"
NEW="$HOME/.claude/skills/.magpie.new.$$"
rm -rf "$NEW"
cp -R "$HERE/skills/magpie" "$NEW"
find "$NEW" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
test -f "$NEW/SKILL.md" && test -f "$NEW/scripts/capture/capture.py" && test -f "$NEW/scripts/doctor.sh"   || { echo "install failed: package incomplete"; rm -rf "$NEW"; exit 1; }
chmod +x "$NEW/scripts/doctor.sh"
rm -rf "$HOME/.claude/skills/magpie"
mv "$NEW" "$HOME/.claude/skills/magpie"
echo "✓ skill installed"

# 2. Library (created if missing; your data files are never overwritten —
#    LIBRARY.md is documentation and IS refreshed each install)
mkdir -p "$LIB/raw" "$LIB/notes" "$LIB/rivals" "$LIB/digests"
for f in CLAUDE.md index.md queue.md log.md; do
  if [ ! -f "$LIB/$f" ]; then cp "$HERE/library-starter/$f" "$LIB/$f"; fi
done
if [ ! -f "$LIB/voice.md" ]; then cp "$HERE/skills/magpie/assets/voice-template.md" "$LIB/voice.md"; fi
cp "$HERE/LIBRARY.md" "$LIB/LIBRARY.md"
echo "✓ library ready at $LIB"

echo
echo "Done. Restart Claude Code, then say:  /magpie doctor   and then   /magpie start"
