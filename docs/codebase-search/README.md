# Answering questions about a codebase without loading the codebase

Research note and working design, August 2026.

## The problem, stated precisely

Asking an agent "how does burn rate get calculated?" has an obvious naive
implementation: put the codebase in the prompt. It fails for two independent
reasons, and the second is the one people miss.

**It doesn't fit.** The Python 3.11 standard library — a fair stand-in for a
mature project — is 673 files, 292,163 lines, about **2.66 million tokens** of
source. That is well past any context window, and paying to re-send it every
turn would be absurd even if it fit.

**Even when it fits, it degrades.** This is the important finding. Model
accuracy is not flat across a long context: performance on multi-document
retrieval follows a U-shaped curve, with accuracy dropping by more than 30%
when the relevant passage sits in the middle rather than at either end, a
result that replicated across six model families
([Liu et al., via Redis](https://redis.io/blog/context-rot/)). Chroma's
follow-up measured the same decay as a general function of input length
([Context Rot](https://www.trychroma.com/research/context-rot)).

For code specifically, the degradation is uneven in a way that shapes the whole
design. *Lexical* recall stays essentially position-independent — frontier
models retrieve a function by exact name at >95% accuracy wherever it sits in
the window. *Semantic* recall does not: accuracy falls by between 16% and 84%
as the target moves toward the middle
([Sense and Sensitivity, arXiv:2505.13353](https://arxiv.org/pdf/2505.13353)).

So a big context window is a reliable place to put an exact name and an
unreliable place to put a concept. That argues directly for a system that does
the *conceptual* work outside the model — narrowing to a handful of candidate
locations — and hands the model only the small, exact region it needs.

The target is not "retrieve better." It is: **answer a question by putting a
few hundred tokens in context instead of a few million, and be able to show
the line that justifies the answer.**

## What the evidence actually supports

### Agentic search beat vector RAG at the thing we care about

The clearest signal comes from SWE-bench, where the task is to locate and fix a
real bug in a real repository. A RAG baseline scored **1.96%**. Replacing
retrieval with tools — grep, file read, directory listing, in a loop —
took SWE-agent to **12.47%**, and by 2026 the Verified leaderboard is dominated
by agentic systems above 80%, none of which rely on vector retrieval over the
target repo ([Morph](https://www.morphllm.com/agentic-search),
[dev.to](https://dev.to/nimay_04/rag-is-not-always-the-answer-anymore-how-ai-agents-search-code-in-2026-43m3)).

The reason is structural, not a matter of embedding quality. Code has
dependency edges, scope, and types that cosine similarity does not model. An
embedding cannot follow a call from `handler.py` into `auth.py` into `jwt.py`;
a symbol index or a call graph does it natively. Retrieval that ignores the
graph is throwing away the most reliable signal in the corpus.

This does not mean embeddings are useless — see *When to add a semantic layer*
below. It means they are the wrong **foundation**.

### Where you do use similarity, hybrid is the floor

Benchmarks converge on the same finding: fusing BM25 with dense retrieval via
Reciprocal Rank Fusion beats either alone across every metric and subset, and a
cross-encoder reranker on the fused candidates beats the embedding-only
baseline again. Hybrid retrieval is "the minimum viable baseline for any RAG
deployment"
([arXiv:2604.01733](https://arxiv.org/html/2604.01733v1),
[Prem AI](https://www.premai.io/blog/hybrid-search-for-rag-bm25-splade-and-vector-search-combined/),
[TianPan](https://tianpan.co/blog/2026-04-12-hybrid-search-production-bm25-dense-embeddings)).
Pure-vector search is the configuration nobody should ship.

### Chunk on syntax, never on character count

Fixed-width splitting cuts functions in half, separating a signature from its
body and dropping the enclosing class. Parsing to an AST and cutting at
function, class, and method boundaries is the correction, and it is what the
[cAST paper](https://arxiv.org/html/2506.15655v1) and every serious
implementation converge on. Chunks should carry their scope chain and
signature, and land around 1000–1500 tokens, because most functions are
300–800 and halving one destroys the logic
([supermemory](https://supermemory.ai/blog/building-code-chunk-ast-aware-code-chunking/),
[sderosiaux](https://sderosiaux.substack.com/p/better-retrieval-beats-better-models)).

### A ranked skeleton is worth more than its size

Aider's repo map is the pattern worth stealing: parse with tree-sitter, extract
the public surface, build a graph where nodes are files and edges are
references, run **PageRank** over it, and serialise the top-ranked symbols into
a token-budgeted map — 1k tokens by default
([aider](https://aider.chat/docs/repomap.html),
[Gandhi](https://anishgandhi.com/aider-pagerank-codebase-ranking/),
[AgentPatterns](https://agentpatterns.ai/context-engineering/repository-map-pattern/)).
Parse, rank, fit. It gives an agent orientation for the price of a short email.

### Retrieve just in time, and isolate the search

Anthropic's context engineering guidance describes the hybrid Claude Code
itself uses: a small amount of context loaded up front (CLAUDE.md), everything
else pulled on demand via glob and grep, with **progressive disclosure** —
each result informing the next query, understanding assembled in layers while
working memory stays small. Sub-agents are progressive disclosure at scale: a
fresh context window does the noisy searching and returns only conclusions
([Anthropic](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents),
[Inkeep](https://inkeep.com/blog/fighting-context-rot)).

## The design

Five layers, cheapest first. Each is independently useful; you do not need the
last one and probably never will.

```
L4  semantic layer (embeddings)      <- optional, add only on measured failure
L3  context isolation (subagents)    <- keeps search out of the conversation
L2  retrieval discipline (skill)     <- teaches the agent to escalate, not dump
L1  structural index (codesearch)    <- symbols, graph, BM25 -- the foundation
L0  legibility (docstrings, CLAUDE.md) <- multiplies everything above it
```

### L0 — Make the code legible

The highest-leverage work is not retrieval. Every layer above ranks on names
and docstrings, so a one-line docstring on each public function is the cheapest
possible improvement to recall; a module named `utils.py` full of undocumented
helpers is invisible to any index. A short `CLAUDE.md` stating the domain
vocabulary — that ULO means unliquidated obligation, that a mart is downstream
of staging — closes the gap between how you ask a question and how the code
spells it.

### L1 — A structural index

Parse the repo into symbols and reference edges, store them in SQLite, and
query with BM25 plus graph rank. This is the layer that replaces "read the
codebase" with "ask where something is." Implementation and measurements below.

### L2 — Retrieval discipline

An index only helps if the agent escalates through it instead of reaching for
`cat`. That is a written procedure — cheapest query first, stop when you can
answer, cite `path:line` — and it belongs in a **skill**, not in CLAUDE.md,
because a skill's body loads only when used while CLAUDE.md is re-sent every
turn ([Claude Code docs](https://code.claude.com/docs/en/skills)).

### L3 — Context isolation

Searching is inherently noisy: dead ends, near-miss hits, files opened and
discarded. A subagent runs it in a separate context window and returns only its
final message, so the main conversation gets the answer without the
transcript ([sub-agents](https://code.claude.com/docs/en/sub-agents)).
Constrain it to read-only tools and give it an explicit output contract.

### L4 — When to add a semantic layer

Add embeddings only when you can point at questions L1–L3 measurably fail on.
The honest failure mode of a lexical index is vocabulary mismatch: asking
"how do we handle money that expired" when the code says `is_expired` and
`period_of_availability` and never uses the word "money."

If you get there, the shape is settled by the evidence above: AST-bounded
chunks, a code-specialised embedding model — general-purpose models lose 5–10
points on code, so `voyage-code-3`, Jina Code, or Nomic Embed Code
([Modal](https://modal.com/blog/6-best-code-embedding-models-compared),
[Milvus](https://milvus.io/blog/choose-embedding-model-rag-2026.md)) — vectors
in `sqlite-vec` alongside the existing tables, and **fused with BM25 via RRF,
never replacing it**. Fusion is the entire point; a vector-only index is
strictly worse than what you already have.

Cost of deferring: near zero, because the chunk boundaries are already correct.
Adding vectors later is an extra column, not a redesign.

## What was built and measured

`tools/codesearch/codesearch.py` implements L1: one file, ~1000 lines, **no
third-party dependencies** — stdlib `ast` and SQLite FTS5 only. That constraint
is deliberate. It means the same command works on your laptop and in a cloud
session with no setup script, no package registry, and nothing to break.

Given your stack, tree-sitter buys nothing: Python's own `ast` module is a
perfect parser for the Python, and dbt's structure lives in `ref()` and
`source()` calls that a regex reads reliably.

| Concern | Choice |
|---|---|
| Chunking | AST boundaries — a chunk is always a whole definition |
| Lexical rank | FTS5 BM25, fields weighted name > docstring > path > body |
| Vocabulary | Identifiers indexed whole **and** split, so `burn rate` hits `compute_burn_rate` |
| Structural rank | PageRank over imports, dbt `ref()`/`source()`, and call targets |
| Map ranking | References ÷ definition count, so `BytesIO` outranks `__init__` |
| Output | Every command takes `--budget` bytes and truncates with a note |

### Measured on the Python 3.11 stdlib (673 files, 292k lines, 2.66M tokens)

| | |
|---|---|
| Full index build | **4.4 s** |
| Staleness check on an unchanged tree | **0.10 s** |
| Ranked repo map | 2,062 bytes ≈ **515 tokens** (0.019% of the corpus) |
| Search, 4 hits with snippets | 1,246 bytes ≈ **311 tokens** (0.012% of the corpus) |
| One-shot context packet | 2,063 bytes ≈ **515 tokens** (0.019% of the corpus) |
| Index on disk | 25 MB, ~2.4× source |

Asked "parse tar header block", the top four hits were
`tarfile.py:1104 create_pax_global_header`, `:1018 create_ustar_header`,
`:1031 create_gnu_header`, and `:1126 _create_header` — the correct answers,
for 311 tokens against a 2.66M-token corpus.

The index being 2.4× source size is real and not slack: `VACUUM` reclaimed only
6%. It is stored chunk bodies plus a full-detail inverted index. At your
repos' scale that is single-digit megabytes, and it is gitignored. If it ever
matters, FTS5 `detail=column` shrinks it at the cost of snippets.

### On a corpus shaped like yours

A synthetic budget-execution repo — `budget/` Python package, `dbt/models/`
staging and marts, `schema.yml`, pytest — was used to check the case that
matters most for your work: a question whose answer **spans Python and SQL**.

"Where do obligations get joined to the spend plan?" returned, in about 1.5 KB,
the Python `compute_burn_rate`, the two staging models, and
`fct_burn_rate.sql` with the `join plan p using (account, fy, month)` line
highlighted. Neither grep for "obligations" nor grep for "spend plan" gets you
that set; the index does, because it ranks a dbt model and a Python function in
the same result list.

PageRank also behaved correctly on the dependency structure: `appropriation.py`
(the most-imported domain module) ranked first, and staging models ranked above
the mart that consumes them.

## Setting up your local environment

Per project — the Budget Execution Tracker first, since it has both Python and
dbt and will show the cross-language win immediately.

One command, from a checkout of this repo:

```bash
tools/codesearch/install.sh --dry-run ~/path/to/budget-execution-tracker
tools/codesearch/install.sh ~/path/to/budget-execution-tracker
```

It is idempotent, merges into an existing `.claude/settings.json` and
`CLAUDE.md` instead of overwriting them, and builds the first index. The
equivalent by hand:

```bash
cd ~/path/to/budget-execution-tracker
mkdir -p tools/codesearch scripts .claude/skills/codesearch .claude/agents

cp <this repo>/tools/codesearch/codesearch.py            tools/codesearch/
cp <this repo>/tools/codesearch/templates/refresh_index.sh scripts/
cp <this repo>/tools/codesearch/templates/SKILL.md       .claude/skills/codesearch/
cp <this repo>/tools/codesearch/templates/codebase-researcher.md .claude/agents/
cp <this repo>/tools/codesearch/templates/settings.json  .claude/settings.json  # merge if it exists

chmod +x scripts/refresh_index.sh
echo ".codesearch/" >> .gitignore
python3 tools/codesearch/codesearch.py index
```

Then append the contents of `templates/CLAUDE.md.example` to your `CLAUDE.md`.

Commit all of it except `.codesearch/`. The index is derived state and belongs
in `.gitignore`; the tool, the skill, the agent, and the hook are source and
must be committed, because that is what makes them appear in a cloud session.

Verify:

```bash
python3 tools/codesearch/codesearch.py stats
python3 tools/codesearch/codesearch.py search "burn rate"
```

## Setting up your cloud environment

The mechanism that matters: **a cloud session is a fresh VM that clones your
repo from GitHub.** Nothing on your laptop reaches it. Your user-level
`~/.claude/settings.json` explicitly does *not* travel — in a cloud session
Claude Code runs hooks from the repository and from server-managed settings
only ([cloud environments](https://code.claude.com/docs/en/cloud-environments)).

That single fact determines the whole cloud story: **everything must be
committed to the repo.**

Because the tool has no dependencies, you need **no setup script at all**.
Python 3 and SQLite are already in the image, and the repo-committed
`.claude/settings.json` hook rebuilds the index after the clone. Setup scripts
are for provisioning the VM — toolchains that aren't pre-installed — and they
run only when no cached environment snapshot exists; SessionStart hooks run on
every session, local and cloud, which is exactly the semantics an index refresh
wants.

If you later add the L4 embedding layer, that is when a setup script earns its
place:

```bash
#!/bin/bash
pip install --quiet sqlite-vec voyageai || true    # exit non-zero fails the session
```

Two constraints to design around: the script must exit zero or the session
fails to start, and it must finish inside about five minutes so the environment
snapshot can build. Network access at the default **Trusted** level covers
PyPI.

Set `VOYAGE_API_KEY` (or equivalent) in the environment's **variables**, not in
the repo.

Sanity-check a cloud session by asking it `/codesearch` questions and confirming
it cites `path:line` rather than dumping files.

**Working across both.** Plan locally in plan mode, push, then
`claude --cloud "..."` to execute; `claude --teleport` pulls a cloud session
back into your terminal with its branch and history. The index rebuilds
correctly on both sides because the hook is in the repo and the rebuild is
deterministic.

## Knowing whether it works

Retrieval systems fail silently — they return *something* plausible. Build a
gold set before you tune anything. Twenty questions is enough:

```
question                                          | must cite
--------------------------------------------------+---------------------------------
how is burn rate calculated                        | budget/marts/burn_rate.py:6
what happens when the spend plan is zero           | budget/marts/burn_rate.py:10
which models feed fct_burn_rate                    | dbt/models/marts/fct_burn_rate.sql:5
where is a treasury account symbol validated       | budget/ingest/loader.py:14
what breaks if Appropriation gains a field         | (refs must list 2 files)
```

Track two numbers per change:

- **recall@5** — is the required file in the top five hits? Below ~0.8, fix
  vocabulary (L0 docstrings) before touching the ranker.
- **tokens to answer** — bytes of tool output ÷ 4. If it climbs, the agent is
  drifting back toward dumping files, and L2 needs tightening.

Tune `--alpha` (graph rank vs BM25, default 0.25) against the gold set, not by
feel. Raise it when searches return plausible-but-peripheral files; lower it
when a well-connected file crowds out the exact match.

## Limits, honestly

- **References resolve by name, not type.** `save()` links to every file
  defining `save()`. This inflates the reference count of common method names;
  the definition-rarity divisor corrects the *map*, not the *edges*. Confirm
  ambiguous names with `refs` before asserting anything.
- **SQL and YAML extraction is regex-based.** Ordinary dbt models parse fine.
  Heavy Jinja macro indirection will not.
- **Rebuilds are full, not incremental.** 4.4 s at 673 files. If a repo ever
  reaches the tens of thousands, per-file incremental updates become worth
  writing; the `sha` column is already there for it.
- **No cross-repo search.** Each repo gets its own index. Answering a question
  spanning the tracker and the parser means querying both.
- **The index can be stale.** `--if-stale` compares content hashes, not
  mtimes, so it is correct, but a rebuild that fails leaves an old index in
  place. The hook reports failure and exits zero rather than blocking a session.
- **This is not a replacement for grep.** For a literal string you already know
  — an error message, a config key — `grep` is faster and exact. The index
  finds *concepts*; grep finds *strings*. Both belong in the ladder.

## What to reach for when

| Situation | Use |
|---|---|
| Repo under ~2k lines | Nothing. Let the agent read it. |
| "Where is X" / "how does Y work" | L1 `search`, then `show PATH:A-B` |
| "What breaks if I change Z" | L1 `refs` — this is the query grep is worst at |
| Cold start on unfamiliar code | L1 `map`, then narrow |
| Question spanning many files | L3 subagent, so the search stays out of the conversation |
| A procedure you keep re-explaining | L2 skill |
| Vocabulary mismatch, measured on a gold set | L4 embeddings, fused with BM25 |
| A literal string you already know | `grep` |

## Sources

- [Effective context engineering for AI agents — Anthropic](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Context Rot: How Increasing Input Tokens Impacts LLM Performance — Chroma](https://www.trychroma.com/research/context-rot)
- [Context rot explained — Redis](https://redis.io/blog/context-rot/)
- [Sense and Sensitivity: Semantic Recall in Long Context Code Reasoning — arXiv:2505.13353](https://arxiv.org/pdf/2505.13353)
- [Agentic Search: How Coding Agents Find the Right Code — Morph](https://www.morphllm.com/agentic-search)
- [RAG Is Not Always the Answer Anymore — dev.to](https://dev.to/nimay_04/rag-is-not-always-the-answer-anymore-how-ai-agents-search-code-in-2026-43m3)
- [Retrieval-Augmented Code Generation: A Survey — arXiv:2510.04905](https://arxiv.org/pdf/2510.04905)
- [cAST: Structural Chunking via Abstract Syntax Tree — arXiv:2506.15655](https://arxiv.org/html/2506.15655v1)
- [code-chunk: AST-Aware Code Chunking — Supermemory](https://supermemory.ai/blog/building-code-chunk-ast-aware-code-chunking/)
- [Better retrieval beats better models for large codebases](https://sderosiaux.substack.com/p/better-retrieval-beats-better-models)
- [Repository map — aider](https://aider.chat/docs/repomap.html)
- [How Aider's repomap uses PageRank — Anish Gandhi](https://anishgandhi.com/aider-pagerank-codebase-ranking/)
- [Repository Map Pattern: AST + PageRank — AgentPatterns](https://agentpatterns.ai/context-engineering/repository-map-pattern/)
- [From BM25 to Corrective RAG: Benchmarking Retrieval Strategies — arXiv:2604.01733](https://arxiv.org/html/2604.01733v1)
- [Hybrid Search for RAG: BM25, SPLADE, and Vector Search — Prem AI](https://www.premai.io/blog/hybrid-search-for-rag-bm25-splade-and-vector-search-combined/)
- [Hybrid Search in Production: Why BM25 Still Wins — TianPan](https://tianpan.co/blog/2026-04-12-hybrid-search-production-bm25-dense-embeddings)
- [6 Best Code Embedding Models Compared — Modal](https://modal.com/blog/6-best-code-embedding-models-compared)
- [Best Embedding Model for RAG 2026 — Milvus](https://milvus.io/blog/choose-embedding-model-rag-2026.md)
- [Extend Claude with skills — Claude Code docs](https://code.claude.com/docs/en/skills)
- [Subagents — Claude Code docs](https://code.claude.com/docs/en/sub-agents)
- [Configure cloud environments — Claude Code docs](https://code.claude.com/docs/en/cloud-environments)
- [Claude Code on the web — Claude Code docs](https://code.claude.com/docs/en/claude-code-on-the-web)
