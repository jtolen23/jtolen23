---
name: codesearch
description: Answer questions about this codebase by querying the local structural index instead of reading files. Use for "where is X", "how does Y work", "what calls Z", "what would break if I change W", and any question that spans more than one file.
allowed-tools: Bash(python3 tools/codesearch/codesearch.py:*), Read, Grep, Glob
---

# Answering questions about this codebase

The index at `.codesearch/index.db` holds every symbol, docstring, and
reference edge in this repo. Query it. Do not read files to orient yourself.

Refresh first if the working tree changed since the last index:
`python3 tools/codesearch/codesearch.py index`

## The ladder — stop as soon as you can answer

1. `codesearch map --budget 2000` — what is this repo, what are the main files.
   Only for a cold start on an unfamiliar area.
2. `codesearch search "<the user's question verbatim>"` — always start here for
   a specific question. Natural language works; the index expands identifiers,
   so "burn rate" matches `compute_burn_rate`.
3. `codesearch symbol NAME` / `codesearch outline PATH` — the signature and
   shape of a specific thing.
4. `codesearch refs NAME` — every call site. This is the blast-radius query;
   run it before proposing any change to a shared function.
5. `codesearch neighbors PATH` — what a file imports and what imports it.
6. `codesearch show PATH:A-B` — read the exact lines, and only those lines.

`codesearch context "<question>"` collapses steps 2–3 into one call when you
just need a starting packet.

## Rules

- Never `cat` or `Read` a whole file to find something in it. Get the line
  range from the index first, then `show PATH:A-B`.
- Cite every claim as `path:line`. If you cannot cite it, you have not
  verified it — go back to step 2.
- The index knows names, not types. It resolves `foo()` to every file defining
  `foo`. When a name is ambiguous, confirm with `refs` before asserting.
- The index is a build artifact, never a source of truth. If a result looks
  stale, re-run `index` and try again.
- `grep`/`Grep` is still the right tool for a literal string you already know
  (an error message, a config key). Use the index to find *concepts*, grep to
  find *strings*.
