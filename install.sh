#!/usr/bin/env bash
# Link every skill in skills/ into each agent's personal skills directory.
#
#   ./install.sh                 # ~/.claude/skills and ~/.codex/skills (the ones whose agent is installed)
#   ./install.sh DIR [DIR...]    # explicit targets, e.g. ~/.agents/skills
#
# Symlinks, so edits here are live everywhere. An existing real directory with
# the same name is left alone and reported - it may be someone's local copy.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)/skills"

if [ $# -gt 0 ]; then targets=("$@"); else
  targets=()
  [ -d "$HOME/.claude" ] && targets+=("$HOME/.claude/skills")
  [ -d "$HOME/.codex" ]  && targets+=("$HOME/.codex/skills")
fi
[ ${#targets[@]} -gt 0 ] || { echo "no agent config dirs found; pass target dirs"; exit 1; }

for t in "${targets[@]}"; do
  mkdir -p "$t"
  for s in "$SRC"/*/; do
    name=$(basename "$s"); dest="$t/$name"
    if [ -e "$dest" ] && [ ! -L "$dest" ]; then
      echo "skip  $dest (a real directory; move it away to link this one)"
    else
      ln -sfn "${s%/}" "$dest"; echo "link  $dest -> ${s%/}"
    fi
  done
done
