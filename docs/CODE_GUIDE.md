# Graphify Code Guide

A reading guide to the codebase, written so you can understand **what each part
does and why**, with concrete examples. It complements `ARCHITECTURE.md` (which
covers the core pipeline contract) by mapping *every* module in `graphify/` and
showing how the pieces fit together.

> New here? Read the [60-second mental model](#60-second-mental-model), then the
> [pipeline walkthrough](#1-the-core-pipeline). Looking for one module? Jump to
> the [module index](#module-index).

---

## 60-second mental model

Graphify turns a codebase (plus docs, PDFs, images, video) into a **knowledge
graph**: nodes are symbols/files/concepts, edges are relationships (`calls`,
`imports`, `inherits`, `references`). Instead of an LLM grepping and reading
whole files, it **queries the graph and pulls back a small, relevant subgraph** —
the headline value is token reduction.

Everything flows through one linear pipeline of pure functions that pass plain
dicts and NetworkX graphs to each other:

```
detect() → extract_<lang>() → build_from_json() → cluster() → god_nodes()/… → generate() → to_json()/…
```

> For a concrete, data-level trace of this pipeline (real node/edge JSON, build
> gotchas, and the MultiGraph ripple fix), see [`docs/DATA_FLOW.md`](./DATA_FLOW.md).

There is no shared state and no side effects outside the output directory
(`graphify-out/` by default). Each stage lives in its own module and can be run
standalone. The `graphify` CLI (`__main__.py`) and the MCP server (`serve.py`)
are the two front doors; everything else is a stage, a subsystem, or an
integration.

---

## Module index

Modules grouped by role. Sizes are a rough guide to where the complexity lives.

### Core pipeline (the seven stages)

> Note: `ARCHITECTURE.md` describes the pipeline with idealized names
> (`collect_files`, `build_graph`, `analyze`, …). The **actual** entry points in
> the code differ — they are listed below. Trust this column.

| Module | Role | Actual entry point(s) |
|--------|------|-----------------------|
| `detect.py` (1197) | Walk the directory, classify & filter files | `detect(root, ...)`, `classify_file(path)` |
| `extract.py` (11134) | Per-file AST/tree-sitter parse → nodes + edges | `extract(path)` |
| `build.py` (444) | Merge per-file extractions into one `nx.Graph` | `build(...)`, `build_from_json(extraction, ...)` |
| `cluster.py` (267) | Community detection; tag each node with a `community` | `cluster(G)` |
| `analyze.py` (712) | God nodes, surprising links, suggested questions, graph diff | `god_nodes(G)`, `surprising_connections(...)`, `suggest_questions(...)`, `graph_diff(G_old, G_new)` |
| `report.py` (218) | Render the human-readable `GRAPH_REPORT.md` | `generate(...)` |
| `export.py` (1390) | Emit graph in many formats | `to_json(...)`, `to_html(...)`, `to_obsidian(...)`, `to_svg(...)`, `to_cypher/to_graphml/to_canvas/push_to_neo4j` |

### Query & read-only inspection

| Module | Role |
|--------|------|
| `serve.py` (993) | Deterministic graph query engine + MCP stdio server (hot-reloads `graph.json`) |
| `affected.py` (151) | Reverse-impact: "what depends on X?" (`graphify affected "X"`) |
| `benchmark.py` (155) | Measure token reduction: full corpus vs. queried subgraph |
| `tree_html.py` (582) | D3 collapsible-tree HTML view |
| `callflow_html.py` (2020) | Mermaid architecture / call-flow diagrams as HTML |

### Git change review (your extension area)

| Module | Role |
|--------|------|
| `diff.py` (463) | Map git-changed files → graph nodes → ripple impact (BFS). `graphify diff` |
| `snapshot.py` (573) | Save/compare graph states over time per commit. `graphify snapshot save\|list\|diff` |
| `prs.py` (748) | Graph-aware PR dashboard: list PRs, per-PR impact, triage |
| `hooks.py` (335) | Install/uninstall git post-commit & post-checkout hooks |

### Graph quality & correctness

| Module | Role |
|--------|------|
| `validate.py` (72) | Enforce the node/edge schema before `build_graph()` consumes it |
| `dedup.py` (425) | Entity deduplication (merge duplicate nodes) |
| `symbol_resolution.py` (538) | Deterministic symbol indexing + conservative cross-file linking |
| `semantic_cleanup.py` (319) | Strip sentence-like "rationale" text so it never becomes a node |
| `diagnostics.py` (390) | Read-only checks for MultiDiGraph readiness |
| `multigraph_compat.py` (212) | Runtime probe for MultiDiGraph mode compatibility |

### Ingestion & external content

| Module | Role |
|--------|------|
| `ingest.py` (331) | Fetch a URL → save into the corpus directory |
| `mcp_ingest.py` (392) | Parse MCP server config files into nodes |
| `scip_ingest.py` (363) | Ingest SCIP index JSON (simplified subset) |
| `transcribe.py` (184) | Video/audio → text transcript (faster-whisper) for extraction |
| `google_workspace.py` (223) | Optional Google Workspace shortcut export support |

### LLM & semantic layer

| Module | Role |
|--------|------|
| `llm.py` (1394) | Direct LLM backend (Claude, Kimi, Gemini, OpenAI) for semantic extraction / labelling |
| `cache.py` (329) | Per-file extraction cache — skip unchanged files on re-run |

### Cross-repo & output extras

| Module | Role |
|--------|------|
| `global_graph.py` (159) | A global graph across all your repos (`~/.graphify/global-graph.json`) |
| `wiki.py` (282) | Wikipedia-style markdown wiki generated from the graph |
| `manifest.py` (4) | Tiny manifest helper |

### Orchestration & live update

| Module | Role |
|--------|------|
| `__main__.py` (4168) | The CLI: argument parsing + dispatch for ~50 commands |
| `watch.py` (883) | Watch a directory and rebuild on file changes |
| `security.py` (336) | Validate every piece of external input (URLs, paths, labels) |

---

## 1. The core pipeline

This is the spine. Follow it once and the rest of the codebase makes sense.

> For a function-by-function deep dive of the capture stages
> (`detect → extract → build → cluster`) with **real captured input/output**, see
> [`docs/PIPELINE_GUIDE.md`](./PIPELINE_GUIDE.md).

### `detect.py` — what counts as input

`detect(root, ...)` walks the directory and returns the set of files worth
extracting; `classify_file(path)` decides each file's `FileType` (source vs.
doc vs. media), and it also converts office formats (PDF/DOCX/XLSX → markdown via
`extract_pdf_text`, `docx_to_markdown`, `xlsx_to_markdown`). This module decides
"this file is in scope" and "what kind of thing it is."

```python
from graphify.detect import detect, classify_file
manifest = detect(Path("."))         # discovers + classifies the corpus
ftype = classify_file(Path("a.py"))  # → FileType for the dispatcher
```

To support a new language you add its suffix here (see *Adding a new language*
in `ARCHITECTURE.md`).

### `extract.py` — the heavy lifter

The largest module by far (11k lines) because it carries a tree-sitter parser
path for each supported language (Python, JS/TS, Java, Go, Rust, C/C++, Ruby,
C#, Kotlin, Scala, PHP, Swift, Lua, …).

`extract(path)` dispatches on file suffix to the right `extract_<lang>` function,
walks the AST to collect symbol **nodes**, then runs a **second call-graph pass**
to add `INFERRED` `calls` edges. The output is always the same schema:

```json
{
  "nodes": [{"id": "...", "label": "...", "source_file": "...", "source_location": "L42"}],
  "edges": [{"source": "...", "target": "...", "relation": "calls", "confidence": "EXTRACTED"}]
}
```

Each `extract_<lang>` follows the same template, so once you read one (start with
the Python path) you can read them all.

### `build.py` — merge into one graph

`build_from_json(extraction, ...)` (and the higher-level `build(...)`) take the
per-file extraction data, run it through `validate.py`, and merge it into a
single `nx.Graph` (deduping nodes by `id`, adding edges). `deduplicate_by_label`
collapses duplicates; `build_merge` and `prefix_graph_for_global` support the
cross-repo global graph. After this stage everything downstream operates on one
graph object.

### `cluster.py` — find the communities

`cluster(G)` runs community detection and writes a `community` attribute onto
every node. Communities are how the rest of the system talks about "areas" of the
codebase (e.g. "the auth community"). The analysis, report, diff, and wiki code
all lean on this attribute.

### `analyze.py` — make the graph interesting

There is no single `analyze()` — instead a set of analysis functions feed the
report:

- **`god_nodes(G)`** — the most-connected nodes (likely central / risky).
- **`surprising_connections(...)`** — edges that cross communities unexpectedly.
- **`suggest_questions(...)`** — starter queries for a user/LLM.
- **`graph_diff(G_old, G_new)`** — a structural graph-vs-graph diff. *Relevant to
  your snapshot work* — `snapshot.py` does something similar over persisted
  snapshots; worth comparing the two implementations.

### `report.py` & `export.py` — outputs

`report.generate(...)` produces `GRAPH_REPORT.md`, the human-readable audit trail
(including `AMBIGUOUS` edges flagged for review). `export.py` is a family of
emitters rather than one function: `to_json()` writes the canonical node-link
`graph.json` that everything else reads, `to_html()` the interactive D3 view,
`to_svg()`, `to_obsidian()` (Obsidian vault), plus `to_cypher()`, `to_graphml()`,
`to_canvas()`, and `push_to_neo4j()`.

---

## 2. Querying the graph

> For a function-by-function walkthrough of the whole *consumption* side
> (`serve.py` query path, `analyze.py`, `report.py`, `export.py`) with real
> captured input/output, see [`docs/QUERY_GUIDE.md`](./QUERY_GUIDE.md).

### `serve.py` — the query engine and MCP server

The heart of read-time behavior. `_query_graph_text()` answers a natural-language
query **deterministically** (no LLM at query time):

1. **Tokenize** the question into terms.
2. **Score nodes** against the terms and pick **seed nodes**.
3. Optionally **filter by edge context** (`--context`, e.g. only `calls` edges).
4. **Traverse** from the seeds — BFS (default) or DFS (`--dfs`) — to `depth` (default 2).
5. **Serialize the subgraph to text** under a `--budget` token cap (default 2000).

`serve(graph_path)` runs this as an MCP stdio server exposing tools like
`query_graph`, `get_node`, `get_neighbors`, `get_community`, `god_nodes`,
`graph_stats`, `shortest_path`, and the PR tools. It **hot-reloads** `graph.json`
when the file changes on disk (mtime + size check), so a running server always
reflects the latest build.

```bash
graphify query "how does auth work?" --depth 2 --budget 2000
graphify explain "validate_url"        # node + neighbors in plain language
graphify path "client" "transport"     # shortest path between two nodes
```

### `affected.py` — reverse impact

`affected_nodes()` does the *reverse* of a normal query: starting from a node, it
walks edges backward to answer "what depends on X?" — the basis of impact
analysis and a building block for `diff.py`.

```bash
graphify affected "build_graph"   # everything that would break if this changed
```

### `benchmark.py` — prove the token savings

`run_benchmark(graph_path)` compares the token cost of feeding the **whole
corpus** to an LLM vs. feeding a **queried subgraph**, so you can quantify the
core value proposition.

---

## 3. Git change review (your extension area)

This is the subsystem you are actively extending. The goal: let an LLM understand
git changes with *fewer tokens* by reasoning over the graph instead of raw diffs.

> For a function-by-function walkthrough of `diff.py` and `snapshot.py` with real
> captured input/output, see [`docs/GIT_REVIEW_GUIDE.md`](./GIT_REVIEW_GUIDE.md).

### `diff.py` — what changed + blast radius

`graphify diff` maps git-changed files → graph nodes → communities, then runs
`ripple_impact()` (BFS from the changed nodes, up to `--depth`) to surface "what
else might be affected." Output is a compact Markdown (or JSON) report grouped by
file and community.

```bash
graphify diff                    # HEAD vs working tree
graphify diff HEAD~3             # last 3 commits
graphify diff main..feature      # branch diff
graphify diff <sha> <sha>        # two explicit commits
graphify diff --staged           # staged only
graphify diff --depth 3 --format json
graphify diff --save             # persist to graphify-out/diff-history/<timestamp>-<ref>.md
```

`diff.py` is clean and well-commented — the git helpers (`_run`) and the
`ripple_impact` traversal are the two pieces to read first.

### `snapshot.py` — architectural evolution over time

Where `diff` answers "what changed in *this* commit," `snapshot` answers "how did
the *graph* evolve between two commits." It saves a compact snapshot (node IDs,
labels, communities, edge index — *not* the full `graph.json`) tagged to a commit
SHA.

```bash
graphify snapshot save              # save snapshot at HEAD
graphify snapshot save <sha>        # label with a specific sha
graphify snapshot list              # list saved snapshots
graphify snapshot diff <a> <b>      # structural diff: added/removed nodes & edges,
graphify snapshot diff HEAD~5 HEAD  #   and community migrations (nodes that moved cluster)
```

Snapshots live in `graphify-out/snapshots/<sha>.json`. The **community migration**
output is the key signal for understanding refactors — a node moving cluster
means its role in the architecture changed.

### `prs.py` — PR-level dashboard

`graphify prs` brings the same graph-aware lens to pull requests: list PRs, show
per-PR graph impact, and triage. (MCP exposes `list_prs`, `get_pr_impact`,
`triage_prs`.) A natural place to wire in the `diff`/`snapshot` output per PR.

### `hooks.py` — automate it

Installs git `post-commit` / `post-checkout` hooks so the graph (and, if you wire
it in, snapshots) update automatically. `hooks.py` detects the right Python
interpreter across pipx/venv/system installs.

```bash
graphify hook install   # / uninstall / status
```

---

## 4. Graph quality & correctness

These modules keep the graph trustworthy.

- **`validate.py`** — the schema gate. `validate_extraction(data)` raises if an
  extraction violates the node/edge contract, *before* it reaches the graph.
- **`dedup.py`** — merges duplicate entities (the same symbol discovered via
  different paths) so the graph isn't littered with near-duplicates.
- **`symbol_resolution.py`** — deterministic symbol indexing and *conservative*
  cross-file resolution (links a call to its definition only when confident).
- **`semantic_cleanup.py`** — when the LLM pass returns sentence-like "rationale"
  text, this converts it into node *attributes* instead of letting it become a
  standalone node. Also enforces validation limits on untrusted agent payloads.
- **`diagnostics.py`** / **`multigraph_compat.py`** — read-only checks and a
  runtime probe for the MultiDiGraph mode (multiple typed edges between the same
  pair of nodes).

---

## 5. Ingestion & external content

Graphify is multi-modal — it ingests more than source code.

- **`ingest.py`** — `ingest(url, ...)` fetches a URL (through `security.py`) and
  saves it into the corpus directory for extraction. Backs `graphify add <url>`
  and `clone <github-url>`.
- **`mcp_ingest.py`** — parses MCP server config files into graph nodes.
- **`scip_ingest.py`** — ingests SCIP index JSON (a precise, compiler-grade symbol
  index) as a simplified subset.
- **`transcribe.py`** — turns video/audio into text via faster-whisper so spoken
  content can be graphed.
- **`google_workspace.py`** — optional Google Workspace shortcut export.

---

## 6. The LLM & semantic layer

- **`llm.py`** — a direct API backend (Claude, Kimi K2.6, Gemini, OpenAI) used
  when *not* running inside Claude Code. The default skill flow uses Claude Code
  subagents; this module is the standalone path (`graphify extract . --backend
  gemini`). It handles file packing, truncation, and token estimation (tiktoken
  with a `1 token ≈ 4 chars` fallback).
- **`cache.py`** — `check_semantic_cache` / `save_semantic_cache` skip re-extracting
  unchanged files on re-run. Also owns the `GRAPHIFY_OUT` env override for the
  output directory (useful for worktrees / shared output).

---

## 7. Cross-repo & output extras

- **`global_graph.py`** — maintains a graph spanning *all* your repos at
  `~/.graphify/global-graph.json` (`global_add` / `global_remove` / `global_list`
  / `global_path`). Backs `graphify global add|remove|list|path`.
- **`wiki.py`** — generates an agent-crawlable Wikipedia-style wiki: an index plus
  one article per community plus god-node articles.
- **`tree_html.py`** / **`callflow_html.py`** — alternate visualizations (D3
  collapsible tree; Mermaid architecture/call-flow).

---

## 8. Orchestration: how a command actually runs

- **`__main__.py`** is the dispatcher. It parses `sys.argv`, matches the first
  token against ~50 commands (`extract`, `query`, `diff`, `snapshot`, `prs`,
  `serve`, the per-editor installers like `claude`/`cursor`/`codex`, etc.), and
  calls into the right module. When adding a command, you register it here *and*
  in the `--help` text.
- **`watch.py`** re-runs the relevant pipeline stages when files change, enabling
  `graphify watch .`.
- **`security.py`** is the input boundary: `validate_url()`, `safe_fetch()`,
  `validate_graph_path()` (must resolve inside the output dir), `sanitize_label()`.
  Every external input is supposed to pass through here.

---

## Where to start reading, by goal

| Your goal | Read in this order |
|-----------|--------------------|
| Understand the whole system | `ARCHITECTURE.md` → this guide → `build.py` → `serve.py` |
| Extend git-change review | `diff.py` → `snapshot.py` → `affected.py` → `prs.py` |
| Add a language | `detect.py` → the Python path in `extract.py` → `validate.py` |
| Improve query quality | `serve.py` (`_query_graph_text`) → `cluster.py` → `analyze.py` |
| Trust/security review | `security.py` → `validate.py` → `semantic_cleanup.py` |

---

*Generated as a code-comprehension aid for the graphify fork. Module sizes and
function names reflect the source at the time of writing — re-check against the
code if a module has since changed.*
