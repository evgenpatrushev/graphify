# Git Change Review — deep-dive guide

This is the subsystem you are building: using the knowledge graph to review git
changes with fewer tokens. Where [`CODE_GUIDE.md`](./CODE_GUIDE.md) maps *every*
module at a glance, this guide goes **function by function** through the two new
modules — `diff.py` and `snapshot.py` — with **real input and output captured
from the graphify repo itself**, so you can see exactly what each part does.

Read order: [the mental model](#mental-model) → [`diff.py`](#1-diffpy--what-changed--blast-radius)
→ [`snapshot.py`](#2-snapshotpy--how-the-graph-evolved) →
[why ripple can be empty](#why-did-ripple-show-0-a-real-teaching-moment).

---

## Mental model

There are **two questions** this subsystem answers, and they are different:

| Module | Question | Compares |
|--------|----------|----------|
| `diff.py` | "What did **this change** touch, and what else depends on it?" | git refs → today's graph |
| `snapshot.py` | "How did the **graph itself** evolve between two commits?" | a saved graph ↔ another saved graph |

`diff` reads the **current** `graph.json` and overlays a git diff on top of it.
`snapshot` saves a small fingerprint of the graph **at a commit** so that later
you can diff two fingerprints and see structural drift (nodes added/removed,
communities re-formed). One is a *live overlay*; the other is *history*.

Both emit Markdown (for an LLM/human to read) or JSON (for tooling).

---

## 1. `diff.py` — what changed + blast radius

The pipeline inside `run_diff()` is a straight line. Each step is one small,
testable function:

```
git diff --name-status   →  match files to graph nodes  →  BFS ripple impact  →  format
   get_changed_files()        nodes_for_files()             ripple_impact()       format_markdown()
```

### 1.1 Reading the git change set — `get_changed_files()`

Turns a git ref expression into a list of `(status, path)` pairs. The four call
shapes map directly onto git invocations:

| You run | Internally |
|---------|------------|
| `graphify diff` | `git diff --name-status HEAD` (all uncommitted) |
| `graphify diff HEAD~3` | `git diff --name-status HEAD~3 HEAD` |
| `graphify diff a b` | `git diff --name-status a b` |
| `graphify diff --staged` | `git diff --cached --name-status` |

`status` is a single git letter — `A`dded, `M`odified, `D`eleted, `R`enamed,
`C`opied. For renames git prints `R100\told\tnew`; the parser keeps the **new**
path (`parts[-1]`). Sibling helpers `get_diff_stat()` (the `+/-` summary) and
`get_commit_messages()` (subjects in range, newest first, capped at 20) gather
the human context that tops the report.

### 1.2 Linking files to the graph — `nodes_for_files()`

A graph node stores its origin in `source_file`. This function inverts that:
for each changed file, it collects every node whose `source_file` matches. The
match is **path-suffix tolerant** (strips leading `./`, accepts either path
ending with the other) so `graphify/diff.py` and `./diff.py` still line up.
Result shape:

```python
{"graphify/extract.py": ["extract.py:extract", "extract.py:extract_python", ...]}
```

### 1.3 The blast radius — `ripple_impact()` (the heart)

This is the headline feature: a breadth-first walk **outward from the changed
nodes** along dependency edges, returning `{node_id: depth}` for everything that
transitively depends on the change.

Two subtleties make it correct:

1. **Direction.** On a directed graph it walks `predecessors` — the nodes that
   *point at* the changed node are the ones that depend on it. (Undirected just
   uses neighbours.)
2. **The MultiGraph fix** — `_edge_relations()`. graphify's `graph.json` loads as
   a NetworkX **MultiGraph**, so `get_edge_data(u, v)` returns a dict *keyed by
   edge-key* (`{0: {relation: "calls"}}`), not a flat `{relation: "calls"}`. The
   original code read `data.get("relation")`, always got `None`, and the relation
   filter rejected every edge → **every diff reported 0 impacted nodes**.
   `_edge_relations()` flattens both shapes into a `set` of relation strings.
   This is covered by `tests/test_diff_ripple.py` (6 cases, all green).

Only "behavioral/structural" relations propagate impact: `calls`, `references`,
`imports`, `imports_from`, `re_exports`, `inherits`, `extends`, `implements`,
`uses`, `mixes_in`, `embeds`. Pure `contains` (file→heading) edges are
deliberately **not** in that set — a doc heading changing doesn't "break" callers.

### 1.4 The data model and formatting

`run_diff()` packs everything into a `DiffReport` dataclass (`ChangedFileEntry`
per file + the ripple maps), then `format_markdown()` / `format_json()` render
it. Markdown groups ripple nodes by depth ("direct dependents", "depth-2
dependents") and lists impacted communities; JSON is the same data unrolled for
tooling. `--save` writes the report to
`graphify-out/diff-history/<timestamp>-<ref>.md`.

### 1.5 Real run

`graphify diff HEAD~3 HEAD --depth 2`, captured against this repo:

```markdown
# Graph-aware diff: HEAD~3..HEAD

## Commits in range
- ec3cb5e feat(dart): modernize AST parser, support nested generics... (#1098)
- a8005c2 feat: add Kilo Code support (#512)
- 4b17f19 drop redundant labels-file write in cluster-only

## Change stats
 graphify/__main__.py     | 471 +++++++++++++++++++++++++++++++-----
 graphify/extract.py      | 538 +++++++++++++++++++++++++++++++++++++++---
 tests/test_dart.py       | 602 ++++++++++++++++++++++++++++++++++++++++++++
 9 files changed, 2208 insertions(+), 110 deletions(-)

## Changed files → graph nodes
### `README.md` (modified)
- README.md
- Prerequisites
- Install
- … and 44 more nodes

## Ripple impact (what else may be affected)
_No upstream dependents found within the configured depth._
```

---

## 2. `snapshot.py` — how the graph evolved

`snapshot` is about *time*. It stores a compact fingerprint of the graph keyed by
commit SHA, then diffs two fingerprints.

### 2.1 What a snapshot contains — `build_snapshot()` / `Snapshot`

It does **not** save the full `graph.json`. It saves only what a structural diff
needs, which keeps files small (hundreds of KB, not MB):

```python
Snapshot(
    sha, short, subject, date,        # git identity of the commit
    node_count, edge_count,           # headline sizes
    nodes={node_id: {label, community, kind, source_file}},
    edges={(src_id, tgt_id): relation},
    communities={community_id: [node_ids]},
)
```

`to_dict()`/`from_dict()` handle JSON's no-tuple-keys limitation by encoding
edge keys as `"src|||tgt"` strings and re-splitting on load. Saved to
`graphify-out/snapshots/<sha>.json`. `load_snapshot()` accepts a full SHA, a
**SHA prefix**, or a path, so `snapshot diff HEAD~5 HEAD` works after refs are
resolved to SHAs by `_cmd_diff`.

### 2.2 The structural diff — `diff_snapshots()`

Pure set math over two snapshots (a = older, b = newer):

- **added / removed nodes** — `ids_b - ids_a` and `ids_a - ids_b`.
- **added / removed edges** — same over the edge-key sets.
- **community migrations** — for nodes present in *both*, where
  `community` differs. This is the most interesting signal: a node moving cluster
  means its role in the architecture changed (a strong refactor smell), even
  though the node itself wasn't added or removed.

`GraphDiff.node_delta` / `edge_delta` give the net size change.
`format_graph_diff_markdown()` leads with a summary table, then groups added
nodes by community, lists community migrations, and samples edge changes (full
edge lists can be huge, so it caps at 20 in Markdown / 200 in JSON).

### 2.3 Real run

`graphify snapshot list` against this repo (one baseline saved at HEAD):

```
SHA            DATE                  NODES   EDGES   SUBJECT
ec3cb5eb3ebd   2026-06-05 08:26:...   1304    1231   feat(dart): modernize AST parser...
```

To see a diff you need two snapshots: `snapshot save`, make commits, rebuild the
graph, `snapshot save` again, then `snapshot diff <old> <new>`.

---

## Why did ripple show 0? (a real teaching moment)

The diff run above reports **75 changed nodes but 0 impacted nodes** — and that
is *correct* for the current graph, not a bug. Two reasons, both worth
internalizing because they shape what this subsystem can tell you:

1. **The changed files map mostly to doc nodes.** `README.md` and
   `skill-kilo.md` contributed most of the 75 nodes, and those are markdown
   *headings*. Headings are joined by `contains` edges, which are intentionally
   excluded from ripple — so they have no dependents to surface.

2. **This graph is `contains`-heavy (AST-only build).** Without an LLM/semantic
   pass, behavioral edges (`calls`, `inherits`, `imports`) are sparse, so even
   the code files that changed (`extract.py`, `__main__.py`) have few graph
   dependents recorded. Run a semantic build and the same diff lights up with
   real blast radius. (On the larger semantic build from a prior session, the
   identical `HEAD~3..HEAD` diff surfaced **>1000** dependents.)

**Takeaway:** ripple impact is only as rich as the graph's behavioral edges.
For meaningful change-review, build the graph with the semantic pass, and judge
ripple by code nodes, not doc nodes. This is the single most useful thing to know
before trusting (or doubting) a `0 impacted nodes` result.

---

## Where this is going (open threads)

- `analyze.py:graph_diff(G_old, G_new)` already does a graph-vs-graph structural
  diff in memory — it **overlaps** `snapshot.py`'s on-disk diff. Worth
  reconciling so there's one implementation (`snapshot diff` could call it).
- Per-PR graph impact in `prs.py` and an LLM **intent summary** per changed
  community are the next features (see [`NEXT_STEPS.md`](./NEXT_STEPS.md)).
- A `post-commit` hook (`hooks.py`) could auto-save snapshots so architectural
  history accrues without manual `snapshot save`.

---

*Companion to [`CODE_GUIDE.md`](./CODE_GUIDE.md) (whole-codebase map) and
[`DATA_FLOW.md`](./DATA_FLOW.md) (pipeline data trace). Examples captured against
the graphify repo at commit `ec3cb5e`; re-run the commands to refresh them.*
