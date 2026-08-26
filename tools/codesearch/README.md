# codesearch

A structural index over a codebase, built so an agent can answer questions
about it while reading a few hundred lines instead of the whole repo.

Single file, no third-party dependencies. Python 3.9+ and SQLite with FTS5
(both already present in Claude Code's cloud environment and on any normal
developer machine).

## Install

Copy `codesearch.py` into your project as `tools/codesearch/codesearch.py`,
then:

```bash
echo ".codesearch/" >> .gitignore     # the index is derived state
python3 tools/codesearch/codesearch.py index
```

## Use

```bash
cs="python3 tools/codesearch/codesearch.py"

$cs map                              # ranked skeleton of the repo
$cs search "how is burn rate calculated"
$cs symbol compute_burn_rate --body 10
$cs refs Appropriation               # blast radius before you change something
$cs outline budget/marts/burn_rate.py
$cs show budget/marts/burn_rate.py:6-10
$cs neighbors budget/marts/burn_rate.py
$cs context "where do obligations join the spend plan"
$cs stats
```

Every command takes `--budget N` (max bytes of output, default 4000) and
truncates with a note rather than flooding a context window. `search` takes
`--json` for scripting.

## What it indexes

| Language | Parser | Symbols extracted |
|---|---|---|
| Python | stdlib `ast` | modules, classes, functions, methods, decorators, constants, imports, call targets |
| SQL / dbt | regex | model name and materialisation, `ref()` and `source()` edges, CTEs, `CREATE TABLE/VIEW/PROC` |
| YAML (dbt) | regex | model, source, seed, snapshot, exposure and macro names |
| Markdown | regex | headings |
| anything else | none | full text, searchable but without symbols |

## How ranking works

1. **BM25** over an FTS5 index of AST-bounded chunks, with field weights
   favouring symbol names and docstrings over bodies. Identifiers are indexed
   both whole and split, so `burn rate` matches `compute_burn_rate`.
2. **PageRank** over the file reference graph (imports, dbt `ref()`, call
   targets), used as a structural prior. Tune the blend with `--alpha`.
3. **Definition rarity** for the repo map: a symbol's reference count is
   divided by the number of files defining that name, so `BytesIO` outranks
   `__init__`.

## Refreshing

`index --if-stale` hashes the tree and exits in ~0.1s when nothing changed;
otherwise it rebuilds. Wire it to a SessionStart hook — see
`templates/refresh_index.sh`.

## Known limits

- Symbol references resolve **by name, not by type**. `save()` links to every
  file defining `save()`. Confirm ambiguous names with `refs` before asserting.
- SQL and YAML extraction is regex-based. It handles ordinary dbt models; it
  will not survive heavy Jinja macro indirection.
- Rebuilds are full, not incremental. At 673 files that is ~4s, which is well
  inside the budget for a session-start hook.
- No embeddings. Purely lexical plus graph structure. See the design note in
  `docs/codebase-search/` for when to add a semantic layer.
