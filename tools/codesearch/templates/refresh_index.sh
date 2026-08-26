#!/usr/bin/env bash
# Keep .codesearch/index.db current. Runs at every session start, local and
# cloud. `--if-stale` hashes the tree and returns immediately when nothing has
# changed, so resumed sessions pay almost nothing.
set -uo pipefail

ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
TOOL="$ROOT/tools/codesearch/codesearch.py"

[ -f "$TOOL" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

if out="$(python3 "$TOOL" --root "$ROOT" index --if-stale 2>&1)"; then
  case "$out" in
    "index up to date") ;;
    *) echo "$out"
       echo "Query it: python3 tools/codesearch/codesearch.py search '<question>'" ;;
  esac
else
  echo "codesearch: index refresh failed, falling back to grep. ${out}" >&2
fi

# Never block a session on a search index.
exit 0
