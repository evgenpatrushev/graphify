# Graphify — Capture Pipeline Guide

A function-by-function deep dive of **how a codebase becomes a graph**: the
`detect → extract → build → cluster` spine that "captures context." Every code
block below is **real input/output captured by running the stages on this repo**
(June 2026), not idealized examples.

This is the companion to [`GIT_REVIEW_GUIDE.md`](./GIT_REVIEW_GUIDE.md) (which
deep-dives the `diff`/`snapshot` subsystem). Where that guide covers *reading*
changes, this one covers *building* the thing you read. For the bird's-eye map of
all modules, see [`CODE_GUIDE.md`](./CODE_GUIDE.md); for a shorter data trace see
[`DATA_FLOW.md`](./DATA_FLOW.md).

> **Why this guide exists:** your stated goal is to reshape *how context is
> captured*. To do that you have to understand the capture pipeline at the level
> of "what each function takes in and hands out." That's what this is.

---

## The shape of the pipeline

```
detect(root)            → manifest dict   (which files, what kind, health checks)
extract_<lang>(path)    → {nodes, edges}  (per-file AST parse → symbols + relations)
extract(paths)          → merged {nodes, edges}  (runs extractors in parallel + caches)
build_from_json(extr)   → nx.Graph        (validate, dedup, assemble one graph)
cluster(G)              → {community: [..]}  (writes `community` attr on each node)
                        → export.to_json() → graphify-out/graph.json
```

Four pure-ish stages. Each takes plain data and returns plain data; the only
side effects are reads of the source tree and the final write to `graphify-out/`.
You can run any stage standalone in a Python REPL — that's exactly how every
example below was produced.

---

## Stage 1 — `detect.py`: what is in scope, and what kind of thing is it

**Entry point:** `detect(root, *, follow_symlinks=None, google_workspace=None, extra_excludes=None) -> dict`

`detect()` walks `root`, decides which files are worth graphing, classifies each
one, and runs corpus health checks. It does **not** read file contents into
nodes — it only builds the manifest the rest of the pipeline consumes.

### Real output

Running `detect(Path("graphify"))` on this repo's package directory returns:

```python
{
  "files": { "code": [ ".../graphify/__init__.py",
                        ".../graphify/__main__.py",
                        ".../graphify/affected.py",
                        ".../graphify/analyze.py", ... ],
             "document": [...], ... },   # grouped by FileType
  "total_files": ...,
  "total_words": ...,
  "needs_graph": ...,        # False if corpus is small enough to not need a graph
  "warning": ...,            # health message, e.g. "you may not need a graph"
  "skipped_sensitive": [...],         # paths skipped because they looked secret
  "graphifyignore_patterns": [...],
  "scan_root": "..."
}
```

The key field is `files`, a **dict keyed by file type**, not a flat list. The
types come from the `FileType` enum:

```python
class FileType(str, Enum):
    CODE = "code"
    DOCUMENT = "document"   # .md .txt .rst .html .yaml ...
    PAPER = "paper"         # .pdf
    IMAGE = "image"         # .png .jpg .svg ...
    VIDEO = "video"         # .mp4 .mp3 ... (transcribed first)
```

### Functions you'll touch

| Function | What it does |
|----------|--------------|
| `detect(root, ...)` | The walk + classify + health-check entry point. Returns the manifest dict above. |
| `classify_file(path)` | Maps a single path → `FileType` by extension (the `*_EXTENSIONS` sets at the top of the module). **This is where you add a new file kind.** |
| `convert_office_file` / `docx_to_markdown` / `xlsx_to_markdown` / `extract_pdf_text` | Office/PDF formats are converted to markdown text *here*, before extraction, so downstream only ever sees code or markdown. |
| `count_words` | Feeds the `total_words` / `needs_graph` health check. |
| `load_manifest` / `save_manifest` | Persist the manifest to `graphify-out/manifest.json`. |
| `detect_incremental` | Re-detect only what changed (used by `watch.py`). |

### Health checks (the part people miss)

`detect()` decides whether a graph is even worth building:

```python
CORPUS_WARN_THRESHOLD = 50_000    # words — below this: "you may not need a graph"
CORPUS_UPPER_THRESHOLD = 500_000  # words — above this: warn about token cost
FILE_COUNT_UPPER = 500            # files — above this: warn about token cost
```

It also drops files that *look* secret (the `skipped_sensitive` list) before they
can ever become nodes — a security gate, not just noise reduction.

> **To reshape capture here:** the in/out filter and the file→type mapping both
> live in `detect.py`. Want to graph a new file kind, or treat a file type
> differently? Start with the `*_EXTENSIONS` sets and `classify_file`.

---

## Stage 2 — `extract.py`: source text → nodes + edges

**Entry points:**
- `extract_<lang>(path) -> dict` — parse **one** file (e.g. `extract_python`).
- `extract(paths, cache_root=None, *, parallel=True, max_workers=None) -> dict` —
  the orchestrator: dispatches each path to the right `extract_<lang>`, runs them
  in parallel, merges, and caches.

There are **43 `extract_*` functions** — one per language/format (python, astro,
bash, blade, c, cpp, csharp, csproj, dart, delphi, dm, …). They all return the
**same schema**, so once you read one you can read them all.

### Real output

Running `extract_python(Path("graphify/diff.py"))` on the repo's own `diff.py`:

```python
{
  "nodes": [
    { "id": "graphify_diff_py",
      "label": "diff.py",
      "file_type": "code",
      "source_file": "graphify/diff.py",
      "source_location": "L1" },
    ... 31 more ...
  ],          # 32 nodes total
  "edges": [...],      # 111 edges
  "raw_calls": [...]   # unresolved call sites, fed to the call-graph pass
}
```

So one ~480-line module yields **32 nodes and 111 edges**. The edge relations
present in just this one file:

```
contains, imports, imports_from, calls, references, rationale_for
```

### The node schema — what's actually there

Note the real fields, because the docs elsewhere are loose about this:

```
id              snake_cased unique key (path + symbol), e.g. "graphify_diff_py"
label           human name, e.g. "diff.py" or "ripple_impact"
file_type       "code" | "document" | "concept"   ← the discriminator
source_file     repo-relative path
source_location "L42"  (line) or null for whole-file/synthetic nodes
```

There is **no `node_type` field** — `file_type` is what distinguishes a code
symbol from a document heading from a concept. (The full-repo `graph.json` bears
this out: every node has `file_type`, none has `node_type`.)

### How each `extract_<lang>` works (the template)

1. Parse the file with its tree-sitter grammar into an AST.
2. Walk the AST, emitting a **node** for each symbol (file, class, function,
   method, …). A per-file `seen_ids` set guarantees each id is emitted **once**.
3. Emit structural **edges** as it goes — `contains` (file→class→method),
   `imports` / `imports_from`.
4. A **second call-graph pass** turns `raw_calls` into `calls` / `references`
   edges where the target can be resolved.

> **Gotcha that has bitten this repo (twice):** the tree-sitter grammar for each
> language is imported **by name** — `tree_sitter_python`, `tree_sitter_javascript`,
> etc. — *not* `tree-sitter-language-pack`. If the package for a language isn't
> installed, that language silently contributes **0 nodes** and the build still
> reports success. The first full build of this repo had 0 Python nodes and looked
> fine. (See [`NEXT_STEPS.md`](./NEXT_STEPS.md) P1: make this loud.)

---

## Stage 3 — `build.py`: many extractions → one graph

**Entry point:** `build_from_json(extraction, *, directed=False, root=None) -> nx.Graph`
(the higher-level `build(...)` wraps it for the full corpus).

This validates the extraction against the schema, then assembles a single
NetworkX graph, deduping as it goes.

### Real output

Feeding the `extract_python(diff.py)` result straight into the builder:

```python
ex = extract.extract_python(Path("graphify/diff.py"))   # 32 nodes, 111 edges
G  = build.build_from_json(ex, root=".")
# → <networkx.Graph>  directed=False  multigraph=False
#   nodes: 32   edges: 77
```

**111 edges in → 77 edges out.** In a simple `Graph`, parallel edges between the
same pair collapse, so the extraction's multiple relations between two symbols
are merged. (The on-disk `graph.json` is loaded back as a **MultiGraph**, which
is what makes `diff.py`'s `_edge_relations()` helper necessary — see the
[GIT_REVIEW_GUIDE](./GIT_REVIEW_GUIDE.md).)

A node keeps its extraction attributes verbatim:

```python
G.nodes["graphify_diff_py"]
# {'label': 'diff.py', 'file_type': 'code',
#  'source_file': 'graphify/diff.py', 'source_location': 'L1'}
```

### Node deduplication — three layers (from the module header)

1. **Within a file (AST):** each extractor's `seen_ids` set emits each id once.
2. **Between files (build):** `G.add_node()` is idempotent — adding the same id
   twice overwrites attributes with the later call. Nodes are added AST-first then
   semantic, so **semantic nodes overwrite AST nodes** (richer labels win, by
   design). Reorder the extractions passed to `build()` to change priority.
3. **Semantic merge (skill):** the skill merges cached + new semantic results on
   `node["id"]` *before* calling `build()`.

### Functions you'll touch

| Function | What it does |
|----------|--------------|
| `build_from_json(extraction, ...)` | Validate + assemble one graph. The function to read first. |
| `build(...)` | Corpus-level wrapper. |
| `deduplicate_by_label` | Collapse near-duplicate nodes that share a label. |
| `build_merge` / `prefix_graph_for_global` / `prune_repo_from_graph` | Support the cross-repo global graph (`global_graph.py`). |
| `validate_extraction` | Re-exported from `validate.py`; the schema gate that runs *before* assembly. |

> **To reshape capture here:** the dedup/overwrite rules above are the heart of
> "what survives into the graph." If AST vs. semantic precedence is wrong for your
> vision, this is the lever.

---

## Stage 4 — `cluster.py`: group nodes into communities

**Entry point:** `cluster(G, resolution=1.0, exclude_hubs_percentile=None) -> dict[int, list[str]]`

`cluster()` runs community detection and **writes a `community` integer attribute
onto every node**. Communities are how the rest of the system names "areas" of the
codebase — diff, report, wiki, and query all read this attribute.

- Tries **Leiden** (graspologic) first — best quality.
- Falls back to **Louvain** (built into networkx) if graspologic isn't installed.
- `resolution > 1.0` → more, smaller communities; `< 1.0` → fewer, larger.
- Splits oversized communities; returns cohesion scores.

### Real output (and an honest caveat)

```python
res = cluster.cluster(G)   # G = the 32-node diff.py graph from Stage 3
# distinct communities written: []   ← none!
```

On a 32-node, single-file graph, clustering produces **no communities** — the
graph is too small / too connected to partition meaningfully. This is expected:
clustering only earns its keep on a full-corpus graph. It also explains a real
property of the current on-disk graph:

```
graphify-out/graph.json:  1305 nodes, 1238 edges, 0 nodes with a `community` attr
relation mix: contains 1180 · method 20 · imports 15 · references 10 · uses 5 · inherits 4 · calls 4
```

That graph was built **without the clustering/semantic pass**, so it's
`contains`-dominated (1180 of 1238 edges) and community-less. This is *why*
`graphify diff` reports rich structural changes but sparse ripple impact on it —
documented at length in the [GIT_REVIEW_GUIDE](./GIT_REVIEW_GUIDE.md). Behavioral
edges (`calls`, `inherits`, `uses`) and communities only become plentiful after a
semantic build.

---

## Putting it together — reproduce every number above

```python
from pathlib import Path
from graphify import detect, extract, build, cluster

man = detect.detect(Path("graphify"))                 # Stage 1: manifest
ex  = extract.extract_python(Path("graphify/diff.py")) # Stage 2: 32 nodes / 111 edges
G   = build.build_from_json(ex, root=".")              # Stage 3: 32 nodes / 77 edges
res = cluster.cluster(G)                               # Stage 4: writes `community`
```

Or, end to end via the CLI (needs the per-language grammar packages installed):

```bash
pip install tree-sitter tree_sitter_python tree_sitter_javascript networkx
graphify update . --no-cluster      # detect → extract → build → graph.json
graphify update .                   # ... + cluster + semantic pass
```

---

## Where the capture decisions live (cheat-sheet)

| If you want to change… | Edit |
|------------------------|------|
| Which files are in scope / what counts as a "kind" | `detect.py` — `*_EXTENSIONS` sets, `classify_file` |
| What office/PDF/video content turns into | `detect.py` converters + `transcribe.py` |
| What symbols/relations a language emits | the relevant `extract_<lang>` in `extract.py` |
| The node/edge schema contract | `validate.py` (`validate_extraction`) |
| AST-vs-semantic precedence, dedup | `build.py` (the three dedup layers) |
| How "areas" are formed | `cluster.py` (`resolution`, hub exclusion) |
| The canonical on-disk format | `export.py` (`to_json` → `graphify-out/graph.json`) |

---

*Captured by running the pipeline on the graphify repo itself, June 2026. Node
counts and schema reflect the source at the time of writing — re-run the
reproduction block above if a module has since changed.*
