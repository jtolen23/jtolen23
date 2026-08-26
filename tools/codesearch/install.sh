#!/usr/bin/env bash
# Install the codesearch kit into a project.
#
#   ./install.sh                 install into the current directory
#   ./install.sh /path/to/repo   install into another repo
#   ./install.sh --dry-run       show what would happen, change nothing
#
# Idempotent: re-running overwrites the tool and templates it owns, and merges
# into .claude/settings.json and CLAUDE.md rather than replacing them.
set -euo pipefail

KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Works from two layouts: the distributed kit (install.sh at the root, sources
# under tools/) and the repo itself (install.sh beside codesearch.py).
if [ -f "$KIT/tools/codesearch.py" ]; then
  SRC_TOOL="$KIT/tools/codesearch.py"
  SRC_DOC="$KIT/tools/README.md"
  SRC_MERGE="$KIT/tools/merge_settings.py"
elif [ -f "$KIT/codesearch.py" ]; then
  SRC_TOOL="$KIT/codesearch.py"
  SRC_DOC="$KIT/README.md"
  SRC_MERGE="$KIT/merge_settings.py"
else
  echo "cannot find codesearch.py next to install.sh" >&2; exit 1
fi
SRC_TPL="$KIT/templates"

DRY=0
TARGET=""

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *)  TARGET="$arg" ;;
  esac
done
TARGET="${TARGET:-$PWD}"

say()  { printf '  %s\n' "$*"; }
run()  { if [ "$DRY" = 1 ]; then return 0; fi; "$@"; }
verb() { if [ "$DRY" = 1 ]; then printf 'would '; fi; }

# ---- preflight -------------------------------------------------------------

[ -d "$TARGET" ] || { echo "no such directory: $TARGET" >&2; exit 1; }
TARGET="$(cd "$TARGET" && pwd)"

command -v python3 >/dev/null 2>&1 || {
  echo "python3 not found. codesearch needs Python 3.9+ and nothing else." >&2; exit 1; }

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || {
  echo "python3 is too old; codesearch needs 3.9 or newer." >&2; exit 1; }

python3 -c 'import sqlite3,sys
try: sqlite3.connect(":memory:").execute("create virtual table t using fts5(a)")
except Exception: sys.exit(1)' || {
  echo "This Python's sqlite3 lacks FTS5, which codesearch requires." >&2; exit 1; }

git -C "$TARGET" rev-parse --git-dir >/dev/null 2>&1 || {
  echo "warning: $TARGET is not a git repo. codesearch falls back to a" >&2
  echo "         filesystem walk; the SessionStart hook still works." >&2; }

echo "Installing codesearch into $TARGET"
[ "$DRY" = 1 ] && echo "(dry run - nothing will be written)"

# ---- files -----------------------------------------------------------------

run mkdir -p "$TARGET/tools/codesearch" "$TARGET/scripts" \
             "$TARGET/.claude/skills/codesearch" "$TARGET/.claude/agents"
run cp "$SRC_TOOL"                     "$TARGET/tools/codesearch/codesearch.py"
run cp "$SRC_DOC"                      "$TARGET/tools/codesearch/README.md"
run cp "$SRC_TPL/refresh_index.sh"     "$TARGET/scripts/refresh_index.sh"
run cp "$SRC_TPL/SKILL.md"             "$TARGET/.claude/skills/codesearch/SKILL.md"
run cp "$SRC_TPL/codebase-researcher.md" "$TARGET/.claude/agents/codebase-researcher.md"
run chmod +x "$TARGET/scripts/refresh_index.sh"
say "$(verb)install tool, skill, subagent and refresh hook"

# ---- .gitignore ------------------------------------------------------------

GI="$TARGET/.gitignore"
if [ -f "$GI" ] && grep -qx '\.codesearch/' "$GI" 2>/dev/null; then
  say ".gitignore already ignores .codesearch/"
elif [ "$DRY" = 1 ]; then
  say "would add .codesearch/ to .gitignore"
else
  printf '\n# codesearch index (derived state, rebuilt at session start)\n.codesearch/\n' >> "$GI"
  say "added .codesearch/ to .gitignore"
fi

# ---- .claude/settings.json -------------------------------------------------

SETTINGS="$TARGET/.claude/settings.json"
if [ ! -f "$SETTINGS" ]; then
  run cp "$SRC_TPL/settings.json" "$SETTINGS"
  say "$(verb)create .claude/settings.json"
elif [ "$DRY" = 1 ]; then
  say "would merge the SessionStart hook into existing .claude/settings.json"
else
  python3 "$SRC_MERGE" "$SETTINGS" "$SRC_TPL/settings.json"
fi

# ---- CLAUDE.md -------------------------------------------------------------

CM="$TARGET/CLAUDE.md"
if [ -f "$CM" ] && grep -q 'codesearch' "$CM" 2>/dev/null; then
  say "CLAUDE.md already mentions codesearch, left alone"
elif [ "$DRY" = 1 ]; then
  say "would append the codesearch section to CLAUDE.md"
else
  # Drop the template's own guidance comment; only the section itself belongs
  # in a CLAUDE.md that gets re-sent every turn.
  { [ -s "$CM" ] && printf '\n'; sed '/^<!--/,/-->$/d' "$SRC_TPL/CLAUDE.md.example" \
      | sed '/[^[:space:]]/,$!d'; } >> "$CM"
  say "appended the codesearch section to CLAUDE.md"
fi

# ---- first index -----------------------------------------------------------

if [ "$DRY" = 1 ]; then
  say "would build the initial index"
else
  echo
  python3 "$TARGET/tools/codesearch/codesearch.py" --root "$TARGET" index
fi

cat <<REPORT

Done. Try it:

  cd $TARGET
  python3 tools/codesearch/codesearch.py map
  python3 tools/codesearch/codesearch.py search "<a question about this repo>"

Commit everything except .codesearch/ - the tool, skill, subagent and hook
have to be in the repo for cloud sessions to pick them up.
REPORT
