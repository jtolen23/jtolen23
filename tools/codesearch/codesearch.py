#!/usr/bin/env python3
"""codesearch - a token-frugal structural index over a codebase.

Design goal: let an LLM agent answer questions about a repository while
reading only a few hundred lines of it. The index stores *where things are*
and *what they are called*; it never asks the model to hold the codebase.

Zero third-party dependencies. Python 3.9+ and SQLite with FTS5 only.

    codesearch index                     build/refresh .codesearch/index.db
    codesearch map      [--budget N]     ranked skeleton of the repo
    codesearch search   QUERY            hybrid BM25 + graph-rank hit list
    codesearch symbol   NAME             definition, signature, docstring
    codesearch refs     NAME             who references it
    codesearch outline  PATH             file skeleton, no bodies
    codesearch show     PATH[:A-B]       exact slice of a file
    codesearch neighbors PATH            1-hop dependency neighbourhood
    codesearch context  QUERY            one-shot budgeted answer packet
    codesearch stats                     index size and coverage
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import textwrap
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

DB_RELPATH = os.path.join(".codesearch", "index.db")
SCHEMA_VERSION = "2"

# Extensions we parse structurally. Everything else is indexed as plain text
# so it is still searchable, just without symbols.
LANG_BY_EXT = {
    ".py": "python",
    ".sql": "sql",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".md": "markdown",
    ".toml": "text",
    ".ini": "text",
    ".cfg": "text",
    ".json": "text",
    ".sh": "text",
    ".txt": "text",
}

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".codesearch",
    "target", ".dbt", "logs", ".tox", "site-packages", ".next", ".terraform",
}

MAX_FILE_BYTES = 400_000        # skip generated blobs
MAX_CHUNK_CHARS = 6_000         # split oversized symbol bodies
GENERIC_CHUNK_LINES = 60        # window for unparsed file types

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "is", "are", "was", "were",
    "how", "what", "where", "which", "who", "why", "when", "do", "does", "did",
    "we", "i", "you", "it", "for", "on", "with", "that", "this", "these", "our",
    "be", "can", "should", "would", "get", "use", "used", "using", "from", "by",
    "at", "as", "if", "any", "all", "into", "about", "code", "codebase", "repo",
}


# --------------------------------------------------------------------------
# repo discovery
# --------------------------------------------------------------------------

def repo_root(start: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", start, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return os.path.abspath(start)


def list_files(root: str) -> List[str]:
    """Prefer git's view of the repo; fall back to a filtered walk."""
    try:
        out = subprocess.run(
            ["git", "-C", root, "ls-files", "-c", "-o", "--exclude-standard"],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode == 0:
            paths = [p for p in out.stdout.splitlines() if p]
            if paths:
                return sorted(set(paths))
    except Exception:
        pass
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dirpath, fn), root)
            found.append(rel)
    return sorted(found)


def read_text(abspath: str) -> Optional[str]:
    try:
        if os.path.getsize(abspath) > MAX_FILE_BYTES:
            return None
        with open(abspath, "rb") as fh:
            raw = fh.read()
        if b"\x00" in raw[:4096]:
            return None
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return None


# --------------------------------------------------------------------------
# identifier handling
# --------------------------------------------------------------------------

_SPLIT_RE = re.compile(r"[_\-./]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def split_ident(name: str) -> str:
    """burn_rateByFund -> 'burn_rateByFund burn rate By Fund'.

    FTS5 tokenises on word boundaries, so a query for 'burn rate' would miss
    the symbol `compute_burn_rate` unless we also index the split form.
    """
    parts = [p for p in _SPLIT_RE.split(name) if p]
    if len(parts) <= 1:
        return name
    return name + " " + " ".join(parts)


# --------------------------------------------------------------------------
# extractors: (symbols, refs) per language
#   symbol: dict(kind, name, qualname, signature, doc, start, end, parent)
#   ref:    (kind, target_name)
# --------------------------------------------------------------------------

def _py_signature(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        try:
            bases = ", ".join(ast.unparse(b) for b in node.bases)
        except Exception:
            bases = ""
        return f"class {node.name}({bases})" if bases else f"class {node.name}"
    try:
        args = ast.unparse(node.args)
    except Exception:
        args = ", ".join(a.arg for a in node.args.args)
    ret = ""
    if getattr(node, "returns", None) is not None:
        try:
            ret = " -> " + ast.unparse(node.returns)
        except Exception:
            ret = ""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({args}){ret}"


def extract_python(text: str) -> Tuple[List[dict], Set[Tuple[str, str]]]:
    symbols: List[dict] = []
    refs: Set[Tuple[str, str]] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return symbols, refs

    mod_doc = (ast.get_docstring(tree) or "").strip().splitlines()
    if mod_doc:
        symbols.append(dict(kind="module", name="__doc__", qualname="__doc__",
                            signature="module", doc=mod_doc[0], start=1, end=1,
                            parent=None))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                refs.add(("import", alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                refs.add(("import", node.module))
            for alias in node.names:
                refs.add(("name", alias.name))
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                refs.add(("call", fn.id))
            elif isinstance(fn, ast.Attribute):
                refs.add(("call", fn.attr))

    def decorators(node) -> List[str]:
        out = []
        for d in getattr(node, "decorator_list", []):
            try:
                out.append("@" + ast.unparse(d))
            except Exception:
                pass
        return out

    def walk_body(body: Sequence[ast.stmt], parent: Optional[str], depth: int) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(node, ast.ClassDef) else (
                    "method" if parent else "func")
                qual = f"{parent}.{node.name}" if parent else node.name
                doc_lines = (ast.get_docstring(node) or "").strip().splitlines()
                sig = _py_signature(node)
                decs = decorators(node)
                if decs:
                    sig = " ".join(decs) + " " + sig
                symbols.append(dict(
                    kind=kind, name=node.name, qualname=qual, signature=sig,
                    doc=doc_lines[0] if doc_lines else "",
                    start=node.lineno,
                    end=getattr(node, "end_lineno", node.lineno),
                    parent=parent,
                ))
                if depth < 2:
                    walk_body(node.body, qual, depth + 1)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and depth == 0:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and (t.id.isupper() or t.id.endswith("_SQL")):
                        symbols.append(dict(
                            kind="const", name=t.id, qualname=t.id, signature=t.id,
                            doc="", start=node.lineno,
                            end=getattr(node, "end_lineno", node.lineno), parent=None))

    walk_body(tree.body, None, 0)
    return symbols, refs


_SQL_REF = re.compile(r"{{\s*ref\(\s*['\"]([^'\"]+)['\"](?:\s*,\s*['\"]([^'\"]+)['\"])?\s*\)\s*}}")
_SQL_SOURCE = re.compile(r"{{\s*source\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*}}")
_SQL_CREATE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?(TABLE|VIEW|PROCEDURE|FUNCTION|INDEX)"
    r"\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\[\]\"`]+)", re.I)
_SQL_CTE = re.compile(r"(?:^|,|\bWITH)\s*([A-Za-z_]\w*)\s+AS\s*\(", re.I | re.M)
_SQL_CONFIG = re.compile(r"materialized\s*=\s*['\"](\w+)['\"]")


def extract_sql(text: str, relpath: str) -> Tuple[List[dict], Set[Tuple[str, str]]]:
    symbols: List[dict] = []
    refs: Set[Tuple[str, str]] = set()
    lines = text.splitlines()

    def line_of(pos: int) -> int:
        return text.count("\n", 0, pos) + 1

    for m in _SQL_REF.finditer(text):
        refs.add(("ref", m.group(2) or m.group(1)))
    for m in _SQL_SOURCE.finditer(text):
        refs.add(("source", f"{m.group(1)}.{m.group(2)}"))

    for m in _SQL_CREATE.finditer(text):
        obj = m.group(2).strip('"`[]')
        symbols.append(dict(kind=m.group(1).lower(), name=obj.split(".")[-1],
                            qualname=obj, signature=f"{m.group(1).upper()} {obj}",
                            doc="", start=line_of(m.start()), end=line_of(m.start()),
                            parent=None))

    # A dbt model's identity is its filename; record it as the primary symbol.
    stem = os.path.splitext(os.path.basename(relpath))[0]
    if "{{" in text or "/models/" in relpath.replace("\\", "/"):
        mat = _SQL_CONFIG.search(text)
        head = next((ln.strip() for ln in lines[:5] if ln.strip().startswith("--")), "")
        symbols.insert(0, dict(
            kind="model", name=stem, qualname=stem,
            signature=f"dbt model {stem}" + (f" ({mat.group(1)})" if mat else ""),
            doc=head.lstrip("- ").strip(), start=1, end=len(lines) or 1, parent=None))

    for m in _SQL_CTE.finditer(text):
        symbols.append(dict(kind="cte", name=m.group(1), qualname=f"{stem}.{m.group(1)}",
                            signature=f"cte {m.group(1)}", doc="",
                            start=line_of(m.start(1)), end=line_of(m.start(1)), parent=stem))
    return symbols, refs


_YML_NAME = re.compile(r"^\s*-\s+name:\s*['\"]?([\w.\-]+)['\"]?\s*$")
_YML_SECTION = re.compile(r"^(\w[\w\-]*):\s*$")


def extract_yaml(text: str) -> Tuple[List[dict], Set[Tuple[str, str]]]:
    """Light structural read of dbt schema.yml / config files.

    Deliberately regex-based: PyYAML is not in the stdlib and this only needs
    the entity names, not a faithful parse.
    """
    symbols: List[dict] = []
    refs: Set[Tuple[str, str]] = set()
    section = ""
    for i, line in enumerate(text.splitlines(), 1):
        sec = _YML_SECTION.match(line)
        if sec:
            section = sec.group(1)
            continue
        m = _YML_NAME.match(line)
        if m and section in {"models", "sources", "seeds", "snapshots", "exposures", "macros"}:
            symbols.append(dict(kind=section.rstrip("s"), name=m.group(1),
                                qualname=m.group(1), signature=f"{section[:-1]} {m.group(1)}",
                                doc="", start=i, end=i, parent=None))
            refs.add(("doc", m.group(1)))
    return symbols, refs


_MD_H = re.compile(r"^(#{1,4})\s+(.+?)\s*$")


def extract_markdown(text: str) -> Tuple[List[dict], Set[Tuple[str, str]]]:
    symbols = []
    for i, line in enumerate(text.splitlines(), 1):
        m = _MD_H.match(line)
        if m:
            symbols.append(dict(kind="heading", name=m.group(2)[:120],
                                qualname=m.group(2)[:120],
                                signature="#" * len(m.group(1)) + " " + m.group(2)[:120],
                                doc="", start=i, end=i, parent=None))
    return symbols, set()


def extract(relpath: str, lang: str, text: str):
    if lang == "python":
        return extract_python(text)
    if lang == "sql":
        return extract_sql(text, relpath)
    if lang == "yaml":
        return extract_yaml(text)
    if lang == "markdown":
        return extract_markdown(text)
    return [], set()


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE, lang TEXT,
  loc INTEGER, sha TEXT, rank REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS symbols(
  id INTEGER PRIMARY KEY, file_id INTEGER, kind TEXT, name TEXT,
  qualname TEXT, signature TEXT, doc TEXT,
  start_line INTEGER, end_line INTEGER, parent TEXT,
  in_degree INTEGER DEFAULT 0, def_count INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS refs(
  src_file_id INTEGER, kind TEXT, target TEXT);
CREATE TABLE IF NOT EXISTS edges(
  src_file_id INTEGER, dst_file_id INTEGER, weight REAL);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, file_id INTEGER, symbol_id INTEGER,
  path TEXT, name TEXT, doc TEXT, body TEXT,
  start_line INTEGER, end_line INTEGER);
CREATE INDEX IF NOT EXISTS ix_sym_name ON symbols(name);
CREATE INDEX IF NOT EXISTS ix_sym_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS ix_ref_target ON refs(target);
CREATE INDEX IF NOT EXISTS ix_chunk_file ON chunks(file_id);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE chunks_fts USING fts5(
  path, name, doc, body,
  content='chunks', content_rowid='id',
  tokenize="unicode61 tokenchars '_'");
"""


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def chunk_file(relpath: str, text: str, symbols: List[dict]) -> List[dict]:
    """Chunk on structural boundaries, never mid-function.

    Bodies come from the symbol's own line range, so a chunk is always a
    complete definition. Files with no extracted symbols fall back to
    blank-line-aligned windows.
    """
    lines = text.splitlines()
    out: List[dict] = []

    body_symbols = [s for s in symbols
                    if s["kind"] in {"func", "method", "class", "model", "view",
                                     "table", "procedure", "function", "const"}
                    and s["end"] >= s["start"]]

    covered: Set[int] = set()
    for s in body_symbols:
        # A class chunk would duplicate its methods; keep the class header only
        # when it has methods of its own.
        start, end = s["start"], min(s["end"], len(lines))
        if s["kind"] == "class":
            child_starts = [c["start"] for c in symbols if c.get("parent") == s["qualname"]]
            if child_starts:
                end = min(end, min(child_starts) - 1)
        body = "\n".join(lines[start - 1:end])
        if not body.strip():
            continue
        for piece_no, off in enumerate(range(0, max(len(body), 1), MAX_CHUNK_CHARS)):
            out.append(dict(
                symbol=s, path=relpath, name=split_ident(s["qualname"]),
                doc=(s["doc"] + " " + s["signature"]).strip(),
                body=body[off:off + MAX_CHUNK_CHARS],
                start=start, end=end, part=piece_no))
        covered.update(range(start, end + 1))

    uncovered = [i for i in range(1, len(lines) + 1) if i not in covered]
    if uncovered and (not body_symbols or len(uncovered) > GENERIC_CHUNK_LINES):
        window: List[int] = []
        for ln in uncovered + [None]:
            if ln is not None and (not window or ln == window[-1] + 1) and len(window) < GENERIC_CHUNK_LINES:
                window.append(ln)
                continue
            if window:
                body = "\n".join(lines[window[0] - 1:window[-1]])
                if body.strip():
                    out.append(dict(symbol=None, path=relpath,
                                    name=split_ident(os.path.basename(relpath)),
                                    doc="", body=body[:MAX_CHUNK_CHARS],
                                    start=window[0], end=window[-1], part=0))
            window = [ln] if ln is not None else []
    return out


def is_stale(root: str, db_path: str) -> bool:
    """True if any indexed file's content hash has moved.

    Hashing every file is far cheaper than parsing them, so this turns a
    session-start refresh into a no-op on an unchanged tree. It compares
    content, not mtimes or `git status`, so a second edit to an
    already-modified file is caught.
    """
    if not os.path.exists(db_path):
        return True
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None or row[0] != SCHEMA_VERSION:
            return True
        stored = {r["path"]: r["sha"] for r in conn.execute("SELECT path, sha FROM files")}
    except sqlite3.OperationalError:
        return True
    finally:
        conn.close()

    current: Dict[str, str] = {}
    for rel in list_files(root):
        parts = rel.replace("\\", "/").split("/")
        if any(p in SKIP_DIRS for p in parts):
            continue
        if os.path.splitext(rel)[1].lower() not in LANG_BY_EXT:
            continue
        text = read_text(os.path.join(root, rel))
        if text is None:
            continue
        current[rel] = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]
    return current != stored


def build_index(root: str, db_path: str, verbose: bool = True) -> dict:
    conn = connect(db_path)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    row = cur.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None or row[0] != SCHEMA_VERSION:
        # An older index has a different table shape; rebuild from scratch
        # rather than trying to migrate a derived artefact.
        for table in ("files", "symbols", "refs", "edges", "chunks", "chunks_fts"):
            cur.execute(f"DROP TABLE IF EXISTS {table}")
    cur.executescript(SCHEMA)
    cur.execute("DROP TABLE IF EXISTS chunks_fts")
    for table in ("files", "symbols", "refs", "edges", "chunks"):
        cur.execute(f"DELETE FROM {table}")

    stats = dict(files=0, skipped=0, symbols=0, chunks=0, refs=0)
    sym_rows: List[tuple] = []
    chunk_rows: List[tuple] = []
    ref_rows: List[tuple] = []

    for rel in list_files(root):
        parts = rel.replace("\\", "/").split("/")
        if any(p in SKIP_DIRS for p in parts):
            continue
        ext = os.path.splitext(rel)[1].lower()
        lang = LANG_BY_EXT.get(ext)
        if lang is None:
            stats["skipped"] += 1
            continue
        text = read_text(os.path.join(root, rel))
        if text is None:
            stats["skipped"] += 1
            continue

        sha = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]
        cur.execute("INSERT INTO files(path, lang, loc, sha) VALUES(?,?,?,?)",
                    (rel, lang, text.count("\n") + 1, sha))
        file_id = cur.lastrowid
        stats["files"] += 1

        symbols, refs = extract(rel, lang, text)
        for s in symbols:
            sym_rows.append((file_id, s["kind"], s["name"], s["qualname"],
                             s["signature"], s["doc"], s["start"], s["end"], s["parent"]))
        for kind, target in refs:
            ref_rows.append((file_id, kind, target))

        for ch in chunk_file(rel, text, symbols):
            chunk_rows.append((file_id, ch["path"], ch["name"], ch["doc"],
                               ch["body"], ch["start"], ch["end"],
                               ch["symbol"]["qualname"] if ch["symbol"] else None))

    cur.executemany(
        "INSERT INTO symbols(file_id,kind,name,qualname,signature,doc,start_line,end_line,parent)"
        " VALUES(?,?,?,?,?,?,?,?,?)", sym_rows)
    cur.executemany("INSERT INTO refs(src_file_id,kind,target) VALUES(?,?,?)", ref_rows)
    stats["symbols"] = len(sym_rows)
    stats["refs"] = len(ref_rows)

    qual_to_sym = {}
    for row in cur.execute("SELECT id, file_id, qualname FROM symbols"):
        qual_to_sym[(row["file_id"], row["qualname"])] = row["id"]

    for (file_id, path, name, doc, body, start, end, qual) in chunk_rows:
        sym_id = qual_to_sym.get((file_id, qual)) if qual else None
        cur.execute(
            "INSERT INTO chunks(file_id,symbol_id,path,name,doc,body,start_line,end_line)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (file_id, sym_id, path, name, doc, body, start, end))
    stats["chunks"] = len(chunk_rows)

    cur.executescript(FTS_SCHEMA)
    cur.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")

    compute_graph(cur)
    conn.commit()
    cur.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('root',?)", (root,))
    cur.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
                (SCHEMA_VERSION,))
    cur.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('stats',?)",
                (json.dumps(stats),))
    conn.commit()
    # A rebuild deletes then re-inserts every row, which leaves the file full of
    # free pages. Reclaim them so the index stays roughly source-sized.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
    conn.close()
    if verbose:
        print(f"indexed {stats['files']} files, {stats['symbols']} symbols, "
              f"{stats['chunks']} chunks, {stats['refs']} refs -> {db_path}")
    return stats


# --------------------------------------------------------------------------
# graph: resolve refs to files, then PageRank
# --------------------------------------------------------------------------

def compute_graph(cur: sqlite3.Cursor, damping: float = 0.85, iterations: int = 25) -> None:
    files = {row["id"]: row["path"] for row in cur.execute("SELECT id, path FROM files")}
    if not files:
        return

    # module path -> file id, so `from budget.marts import burn` resolves.
    module_index: Dict[str, int] = {}
    for fid, path in files.items():
        norm = path.replace("\\", "/")
        stem = re.sub(r"\.(py|sql|yml|yaml|md)$", "", norm)
        module_index.setdefault(stem.replace("/", "."), fid)
        module_index.setdefault(os.path.basename(stem), fid)

    # symbol name -> defining file ids (a name can be defined in several files)
    def_index: Dict[str, List[int]] = {}
    for row in cur.execute("SELECT file_id, name, qualname FROM symbols"):
        for key in {row["name"], row["qualname"]}:
            def_index.setdefault(key, []).append(row["file_id"])

    edge_weight: Dict[Tuple[int, int], float] = {}
    in_degree: Dict[str, int] = {}

    for row in cur.execute("SELECT src_file_id, kind, target FROM refs"):
        src, kind, target = row["src_file_id"], row["kind"], row["target"]
        in_degree[target] = in_degree.get(target, 0) + 1
        dsts: List[int] = []
        if kind == "import":
            for candidate in (target, target.rsplit(".", 1)[0], target.rsplit(".", 1)[-1]):
                if candidate in module_index:
                    dsts = [module_index[candidate]]
                    break
        elif kind in ("ref", "source", "doc"):
            if target in module_index:
                dsts = [module_index[target]]
            elif target in def_index:
                dsts = def_index[target][:3]
        else:
            dsts = def_index.get(target, [])[:3]
        # An import edge is a hard dependency; a bare call is weaker evidence.
        w = 1.0 if kind in ("import", "ref") else 0.4
        for dst in dsts:
            if dst != src:
                edge_weight[(src, dst)] = edge_weight.get((src, dst), 0.0) + w

    cur.executemany("INSERT INTO edges(src_file_id,dst_file_id,weight) VALUES(?,?,?)",
                    [(s, d, w) for (s, d), w in edge_weight.items()])

    out_sum: Dict[int, float] = {}
    for (s, _d), w in edge_weight.items():
        out_sum[s] = out_sum.get(s, 0.0) + w

    n = len(files)
    rank = {fid: 1.0 / n for fid in files}
    for _ in range(iterations):
        nxt = {fid: (1.0 - damping) / n for fid in files}
        dangling = sum(rank[f] for f in files if out_sum.get(f, 0.0) == 0.0)
        for fid in files:
            nxt[fid] += damping * dangling / n
        for (s, d), w in edge_weight.items():
            nxt[d] += damping * rank[s] * (w / out_sum[s])
        rank = nxt

    cur.executemany("UPDATE files SET rank=? WHERE id=?",
                    [(r, fid) for fid, r in rank.items()])
    cur.executemany("UPDATE symbols SET in_degree=? WHERE name=?",
                    [(v, k) for k, v in in_degree.items()])
    # A name defined in 200 files (`get`, `append`, `__init__`) carries almost no
    # information about what a file is *for*. Divide references by definition
    # count so the map surfaces distinctive symbols, not ubiquitous ones.
    cur.executemany("UPDATE symbols SET def_count=? WHERE name=?",
                    [(len(set(fids)), name) for name, fids in def_index.items()])


# --------------------------------------------------------------------------
# output budgeting - the whole point is that answers stay small
# --------------------------------------------------------------------------

class Budget:
    def __init__(self, limit_bytes: int):
        self.limit = limit_bytes
        self.used = 0
        self.truncated = 0

    def emit(self, line: str = "") -> bool:
        cost = len(line.encode("utf-8")) + 1
        if self.used + cost > self.limit:
            self.truncated += 1
            return False
        self.used += cost
        print(line)
        return True

    def finish(self) -> None:
        if self.truncated:
            print(f"... [{self.truncated} more lines withheld; "
                  f"raise --budget or narrow the query]")


def open_db(root: str) -> sqlite3.Connection:
    db = os.path.join(root, DB_RELPATH)
    if not os.path.exists(db):
        sys.exit(f"no index at {db} - run: codesearch index")
    conn = connect(db)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is None or row[0] != SCHEMA_VERSION:
        sys.exit(f"index at {db} is stale (schema v{row[0] if row else '?'}, "
                 f"expected v{SCHEMA_VERSION}) - run: codesearch index")
    return conn


def match_expr(query: str, raw: bool) -> str:
    if raw:
        return query
    terms = re.findall(r"[A-Za-z_][\w]*|\"[^\"]+\"", query)
    kept = [t for t in terms if t.lower() not in STOPWORDS]
    if not kept:
        kept = terms or [query]
    out = []
    for t in kept:
        if t.startswith('"'):
            out.append(t)
        else:
            out.append('"' + t.replace('"', "") + '"')
            # match compound identifiers too: 'burn' hits compute_burn_rate
            out.append('"' + t.replace('"', "") + '"*')
    return " OR ".join(out)


def normalise(values: Sequence[float]) -> List[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return [1.0] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def run_search(conn, query: str, limit: int, raw: bool, alpha: float) -> List[dict]:
    """Hybrid rank: FTS5 BM25 for the query, PageRank as a structural prior."""
    expr = match_expr(query, raw)
    sql = """
      SELECT c.id, c.path, c.name, c.doc, c.start_line, c.end_line,
             f.rank AS prank, f.lang, s.kind AS kind, s.qualname AS qualname,
             s.signature AS signature, s.doc AS sym_doc,
             bm25(chunks_fts, 4.0, 8.0, 3.0, 1.0) AS bm,
             snippet(chunks_fts, 3, '>>', '<<', ' ... ', 14) AS snip
      FROM chunks_fts
      JOIN chunks c ON c.id = chunks_fts.rowid
      JOIN files  f ON f.id = c.file_id
      LEFT JOIN symbols s ON s.id = c.symbol_id
      WHERE chunks_fts MATCH ?
      ORDER BY bm
      LIMIT ?
    """
    try:
        rows = list(conn.execute(sql, (expr, limit * 6)))
    except sqlite3.OperationalError as exc:
        sys.exit(f"bad query {expr!r}: {exc}")
    if not rows:
        return []
    bm_scores = normalise([-r["bm"] for r in rows])
    pr_scores = normalise([r["prank"] for r in rows])
    out = []
    for row, bm, pr in zip(rows, bm_scores, pr_scores):
        out.append(dict(row) | {"score": round((1 - alpha) * bm + alpha * pr, 4)})
    out.sort(key=lambda r: -r["score"])
    # one hit per symbol keeps the list diverse instead of ten chunks of one file
    seen, deduped = set(), []
    for r in out:
        key = (r["path"], r["qualname"] or r["start_line"] // 40)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
        if len(deduped) >= limit:
            break
    return deduped


def cmd_search(args, conn) -> None:
    hits = run_search(conn, args.query, args.limit, args.raw, args.alpha)
    if args.json:
        print(json.dumps(hits, indent=2))
        return
    b = Budget(args.budget)
    if not hits:
        print(f"no hits for {args.query!r}")
        return
    b.emit(f"# {len(hits)} hits for {args.query!r}")
    for h in hits:
        label = f"{h['kind']} {h['qualname']}" if h["qualname"] else h["lang"]
        b.emit(f"\n{h['path']}:{h['start_line']}-{h['end_line']}  {label}  [{h['score']}]")
        if h["signature"]:
            b.emit(f"    {h['signature'][:160]}")
        doc = h["sym_doc"] or h["doc"]
        if doc and doc != h["signature"]:
            b.emit(f"    {doc[:160]}")
        snip = " ".join(h["snip"].split())
        if snip:
            b.emit(f"    ~ {snip[:220]}")
    b.finish()


def cmd_map(args, conn) -> None:
    b = Budget(args.budget)
    files = list(conn.execute(
        "SELECT id, path, lang, loc, rank FROM files ORDER BY rank DESC, loc DESC"))
    total = sum(f["loc"] for f in files)
    b.emit(f"# repo map: {len(files)} indexed files, {total} lines, "
           f"ranked by reference graph")
    for f in files:
        if args.lang and f["lang"] != args.lang:
            continue
        syms = list(conn.execute(
            "SELECT kind, qualname, signature, doc, in_degree, def_count, start_line"
            " FROM symbols WHERE file_id=? AND kind NOT IN ('cte','heading','module')"
            "   AND substr(name,1,2) <> '__'"
            " ORDER BY (in_degree*1.0)/def_count DESC, start_line LIMIT ?",
            (f["id"], args.per_file)))
        if not syms and f["lang"] not in ("markdown",):
            continue
        if not b.emit(f"\n{f['path']}  ({f['loc']} loc, rank {f['rank']:.4f})"):
            break
        exhausted = False
        for s in syms:
            deg = f" <{s['in_degree']}" if s["in_degree"] else ""
            line = f"  :{s['start_line']:<5} {s['signature'][:150]}{deg}"
            if s["doc"]:
                line += f"  - {s['doc'][:70]}"
            if not b.emit(line):
                exhausted = True
                break
        if exhausted:
            break
    b.finish()


def cmd_symbol(args, conn) -> None:
    b = Budget(args.budget)
    rows = list(conn.execute(
        "SELECT s.*, f.path FROM symbols s JOIN files f ON f.id=s.file_id"
        " WHERE s.name=? OR s.qualname=? ORDER BY s.in_degree DESC LIMIT 10",
        (args.name, args.name)))
    if not rows:
        rows = list(conn.execute(
            "SELECT s.*, f.path FROM symbols s JOIN files f ON f.id=s.file_id"
            " WHERE s.name LIKE ? ORDER BY s.in_degree DESC LIMIT 10",
            (f"%{args.name}%",)))
    if not rows:
        print(f"no symbol matching {args.name!r}")
        return
    for r in rows:
        b.emit(f"{r['path']}:{r['start_line']}-{r['end_line']}  {r['kind']} {r['qualname']}"
               f"  (referenced {r['in_degree']}x)")
        if r["signature"]:
            b.emit(f"    {r['signature'][:200]}")
        if r["doc"]:
            b.emit(f"    {r['doc'][:200]}")
        if args.body:
            body = conn.execute(
                "SELECT body FROM chunks WHERE symbol_id=? ORDER BY id LIMIT 1",
                (r["id"],)).fetchone()
            if body:
                for line in body["body"].splitlines()[:args.body]:
                    if not b.emit("    | " + line[:200]):
                        break
        b.emit()
    b.finish()


def cmd_refs(args, conn) -> None:
    b = Budget(args.budget)
    files = list(conn.execute(
        "SELECT f.path, group_concat(DISTINCT r.kind) AS kinds FROM refs r"
        " JOIN files f ON f.id=r.src_file_id"
        " WHERE r.target=? OR r.target LIKE ? GROUP BY f.path ORDER BY f.path",
        (args.name, f"%.{args.name}")))
    b.emit(f"# files referencing {args.name!r}: {len(files)}")
    for f in files:
        b.emit(f"  {f['path']}  ({f['kinds']})")
    rows = list(conn.execute(
        "SELECT c.path, c.body, c.start_line FROM chunks_fts"
        " JOIN chunks c ON c.id=chunks_fts.rowid WHERE chunks_fts MATCH ? LIMIT 200",
        (f'body : "{args.name}"',)))
    b.emit("\n# call sites")
    shown = 0
    for r in rows:
        for i, line in enumerate(r["body"].splitlines()):
            if re.search(rf"\b{re.escape(args.name)}\b", line):
                if not b.emit(f"  {r['path']}:{r['start_line'] + i}: {line.strip()[:150]}"):
                    b.finish()
                    return
                shown += 1
                if shown >= args.limit:
                    b.finish()
                    return
    b.finish()


def cmd_outline(args, conn) -> None:
    b = Budget(args.budget)
    f = conn.execute("SELECT * FROM files WHERE path=?", (args.path,)).fetchone()
    if not f:
        f = conn.execute("SELECT * FROM files WHERE path LIKE ?",
                         (f"%{args.path}%",)).fetchone()
    if not f:
        sys.exit(f"not indexed: {args.path}")
    b.emit(f"# {f['path']}  ({f['loc']} loc, {f['lang']}, rank {f['rank']:.4f})")
    for s in conn.execute(
            "SELECT * FROM symbols WHERE file_id=? ORDER BY start_line", (f["id"],)):
        indent = "  " if s["parent"] else ""
        line = f"{indent}:{s['start_line']:<5} {s['signature'][:170]}"
        if s["doc"]:
            line += f"  - {s['doc'][:70]}"
        if not b.emit(line):
            break
    b.finish()


def cmd_show(args, conn) -> None:
    root = conn.execute("SELECT value FROM meta WHERE key='root'").fetchone()["value"]
    spec = args.path
    lo = hi = None
    m = re.match(r"^(.*?):(\d+)-(\d+)$", spec)
    if m:
        spec, lo, hi = m.group(1), int(m.group(2)), int(m.group(3))
    row = conn.execute("SELECT path FROM files WHERE path=? OR path LIKE ?",
                       (spec, f"%{spec}%")).fetchone()
    if not row:
        sys.exit(f"not indexed: {spec}")
    text = read_text(os.path.join(root, row["path"])) or ""
    lines = text.splitlines()
    lo = lo or 1
    hi = hi or min(len(lines), lo + 120)
    b = Budget(args.budget)
    b.emit(f"# {row['path']}:{lo}-{hi}")
    for i in range(lo, min(hi, len(lines)) + 1):
        if not b.emit(f"{i:>5}  {lines[i - 1]}"):
            break
    b.finish()


def cmd_neighbors(args, conn) -> None:
    b = Budget(args.budget)
    f = conn.execute("SELECT * FROM files WHERE path=? OR path LIKE ?",
                     (args.path, f"%{args.path}%")).fetchone()
    if not f:
        sys.exit(f"not indexed: {args.path}")
    b.emit(f"# {f['path']} (rank {f['rank']:.4f})")
    b.emit("\n## depends on")
    for r in conn.execute(
            "SELECT f.path, e.weight FROM edges e JOIN files f ON f.id=e.dst_file_id"
            " WHERE e.src_file_id=? ORDER BY e.weight DESC LIMIT ?", (f["id"], args.limit)):
        b.emit(f"  -> {r['path']}  ({r['weight']:.1f})")
    b.emit("\n## depended on by")
    for r in conn.execute(
            "SELECT f.path, e.weight FROM edges e JOIN files f ON f.id=e.src_file_id"
            " WHERE e.dst_file_id=? ORDER BY e.weight DESC LIMIT ?", (f["id"], args.limit)):
        b.emit(f"  <- {r['path']}  ({r['weight']:.1f})")
    b.finish()


def cmd_context(args, conn) -> None:
    """One-shot packet: the hits, plus the shape of the files they live in."""
    hits = run_search(conn, args.query, args.limit, False, args.alpha)
    if not hits:
        print(f"no hits for {args.query!r}")
        return
    b = Budget(args.budget)
    b.emit(f"# context packet for {args.query!r}")
    seen_files: List[str] = []
    for h in hits:
        if h["path"] not in seen_files:
            seen_files.append(h["path"])
    for path in seen_files[:args.files]:
        f = conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        if not b.emit(f"\n## {path}  ({f['loc']} loc, {f['lang']})"):
            break
        for s in conn.execute(
                "SELECT * FROM symbols WHERE file_id=? AND kind NOT IN ('cte','heading')"
                " ORDER BY start_line LIMIT 25", (f["id"],)):
            b.emit(f"   :{s['start_line']:<5} {s['signature'][:140]}")
        for h in [x for x in hits if x["path"] == path][:2]:
            b.emit(f"   >> match at :{h['start_line']} "
                   f"{' '.join(h['snip'].split())[:200]}")
    b.emit("\n# next: codesearch show PATH:A-B  |  codesearch refs NAME")
    b.finish()


def cmd_stats(args, conn) -> None:
    meta = conn.execute("SELECT value FROM meta WHERE key='stats'").fetchone()
    print(json.dumps(json.loads(meta["value"]) if meta else {}, indent=2))
    print("\nby language:")
    for r in conn.execute(
            "SELECT lang, count(*) n, sum(loc) loc FROM files GROUP BY lang ORDER BY loc DESC"):
        print(f"  {r['lang']:<10} {r['n']:>4} files  {r['loc']:>7} loc")
    print("\ntop-ranked files:")
    for r in conn.execute("SELECT path, rank FROM files ORDER BY rank DESC LIMIT 10"):
        print(f"  {r['rank']:.4f}  {r['path']}")
    db = os.path.join(args.root, DB_RELPATH)
    print(f"\nindex size: {os.path.getsize(db) / 1024:.0f} KiB")


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS,
                        help="repo root (default: git toplevel)")
    common.add_argument("--budget", type=int, default=argparse.SUPPRESS,
                        help="max bytes of output (default 4000)")

    p = argparse.ArgumentParser(prog="codesearch", description=__doc__,
                                parents=[common],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, **kw):
        return sub.add_parser(name, parents=[common], **kw)

    ix = add("index", help="build or refresh the index")
    ix.add_argument("--if-stale", action="store_true",
                    help="skip the rebuild when no indexed file has changed")

    s = add("search", help="hybrid BM25 + graph-rank search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=8)
    s.add_argument("--alpha", type=float, default=0.25,
                   help="weight of graph rank vs BM25 (0..1)")
    s.add_argument("--raw", action="store_true", help="pass query straight to FTS5")
    s.add_argument("--json", action="store_true")

    m = add("map", help="ranked repo skeleton")
    m.add_argument("--per-file", type=int, default=6)
    m.add_argument("--lang")

    sy = add("symbol", help="look up a definition")
    sy.add_argument("name")
    sy.add_argument("--body", type=int, default=0, help="also print N lines of body")

    r = add("refs", help="find references")
    r.add_argument("name")
    r.add_argument("--limit", type=int, default=40)

    o = add("outline", help="file skeleton without bodies")
    o.add_argument("path")

    sh = add("show", help="print an exact slice, PATH or PATH:A-B")
    sh.add_argument("path")

    nb = add("neighbors", help="1-hop dependency neighbourhood")
    nb.add_argument("path")
    nb.add_argument("--limit", type=int, default=12)

    c = add("context", help="one-shot budgeted answer packet")
    c.add_argument("query")
    c.add_argument("--limit", type=int, default=10)
    c.add_argument("--files", type=int, default=4)
    c.add_argument("--alpha", type=float, default=0.25)

    add("stats", help="index size and coverage")

    # Behave like a normal unix filter when piped into `head` or `less`.
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass  # SIGPIPE does not exist on Windows

    args = p.parse_args(argv)
    args.budget = getattr(args, "budget", 4000)
    args.root = repo_root(getattr(args, "root", "."))
    db_path = os.path.join(args.root, DB_RELPATH)

    if args.cmd == "index":
        if getattr(args, "if_stale", False) and not is_stale(args.root, db_path):
            print("index up to date")
            return
        build_index(args.root, db_path)
        return

    conn = open_db(args.root)
    try:
        {
            "search": cmd_search, "map": cmd_map, "symbol": cmd_symbol,
            "refs": cmd_refs, "outline": cmd_outline, "show": cmd_show,
            "neighbors": cmd_neighbors, "context": cmd_context, "stats": cmd_stats,
        }[args.cmd](args, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        # Reached only where SIGPIPE is unavailable. Redirect stdout to devnull
        # so the interpreter's shutdown flush cannot raise a second time.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
