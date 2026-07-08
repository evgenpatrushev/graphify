# Query Guide — how the graph is *consumed*

> Companion to [PIPELINE_GUIDE.md](PIPELINE_GUIDE.md), which covers how the
> graph is *built* (`detect → extract → build → cluster`). This guide covers the
> other half: once `graphify-out/graph.json` exists, how does anything actually
> get an answer out of it?
>
> Every example below was captured by running the real functions against the
> repo's own `graphify-out/graph.json` (a **MultiDiGraph, 1311 nodes / 1238
> edges**, AST-only — so it is `contains`-dominated and has **0 communities**;
> see the note at the end about what changes on a semantic build).

The consumption side is four modules:

| Module | Role | Headline entry points |
|---|---|---|
| `serve.py` | **Answer a natural-language question** with a token-budgeted slice of the graph. This is the "LLM understands the codebase with fewer tokens" path. | `serve()`, `_query_graph_text()` |
| `analyze.py` | **Mine the graph for structure** — hubs, surprising links, good questions, snapshot diffs. | `god_nodes`, `surprising_connections`, `suggest_questions`, `graph_diff` |
| `report.py` | **Render a human Markdown report** from analysis outputs. | `generate()` |
| `export.py` | **Serialize the graph** to other formats (HTML, Cypher, Obsidian, GraphML, …). | `to_json`, `to_html`, `to_cypher`, … |

---

## 1. `serve.py` — the question-answering path

This is the most important module for Eugene's goal (give an LLM understanding
with fewer tokens). `serve()` is a stdin/stdout loop, but the reusable engine is
`_query_graph_text(G, question, depth, token_budget, …)`. It runs a small
five-stage pipeline. Here is each stage with **real output** for the question
`"how does ripple impact work in diff"`.

### Stage 1 — `_query_terms(question)` → searchable tokens

```python
serve._query_terms("how does ripple impact work in diff")
# -> ['how', 'does', 'ripple', 'impact', 'work', 'diff']
```

Lowercases, strips punctuation, segments (it even handles Chinese via
`_segment_chinese`), and drops terms that aren't searchable (`_is_searchable`).
No stopword list is applied here — ranking handles common words in stage 3.

### Stage 2 — `_compute_idf(G, terms)` → rarity weights

Inverse document frequency over node labels: a term that appears in few node
labels (like `ripple`) gets a high weight; a term in many labels (like `how`)
gets a low one. This is what stops generic words from dominating.

### Stage 3 — `_score_nodes(G, terms)` → ranked node list

Each node is scored by how many query terms match its label, weighted by IDF:

```
471.483  docs_how_it_works_how_graphify_works
471.483  docs_how_it_works_how_community_detection_works
471.483  docs_how_it_works
464.621  rsl_siege_manager_readme_how_to_reproduce
464.621  raw_architecture_how_data_flows
```

(On this AST-only graph the top hits are doc-heading nodes — there is no
`ripple_impact` *function* node because behavioral nodes are sparse without a
semantic build. The scoring itself is working correctly.)

### Stage 4 — `_pick_seeds(scored, max_k=3, gap_ratio=0.2)` → entry points

Takes the top-scoring nodes as BFS seeds, but stops early when the score drops
off a cliff (`gap_ratio`) so a single strong match doesn't drag in three weak
ones:

```python
['docs_how_it_works_how_graphify_works',
 'docs_how_it_works_how_community_detection_works',
 'docs_how_it_works']
```

### Stage 5 — `_bfs(G, seeds, depth)` then `_subgraph_to_text(...)`

`_bfs` expands `depth` hops out from the seeds (there is also `_dfs` for
narrow-deep traversal). Then `_subgraph_to_text` flattens that subgraph into a
compact, **token-budgeted** textual form — `NODE`/`EDGE` lines — that is what
you hand to the LLM instead of raw files:

```
$ depth=2, token_budget=400
bfs subgraph: 9 nodes 6 edges

NODE How graphify works [src=docs/how-it-works.md loc=L1 community=]
NODE The three passes [src=docs/how-it-works.md loc=L3 community=]
NODE Parallel extraction [src=docs/how-it-works.md loc=L71 community=]
...
EDGE How graphify works --contains [EXTRACTED]--> The three passes
EDGE How graphify works --contains [EXTRACTED]--> Parallel extraction
```

The `token_budget` is the lever for the "fewer tokens" promise: the subgraph is
truncated to fit the budget, and `seeds` are prioritized so the most relevant
nodes survive truncation. Each line carries provenance (`src=`, `loc=`) so the
LLM can cite or jump to the source.

> **Context filters.** Before BFS, `_resolve_context_filters` /
> `_filter_graph_by_context` can narrow the graph to a subsystem (inferred from
> the question or passed explicitly), so a query about "auth" doesn't traverse
> into unrelated clusters.

---

## 2. `analyze.py` — structural mining

These functions don't need a question; they read the whole graph and surface
structure. Real output against the repo graph below.

### `god_nodes(G, top_n=10)` — the hubs

Highest-degree nodes — the things everything else touches. Often the right place
to start understanding (or the right thing to worry about in a refactor):

```python
analyze.god_nodes(G, top_n=5)
# [{"id": "changelog_changelog", "label": "Changelog", "degree": 120},
#  {"id": "...graph_report_communities_141_total...", "label": "Communities (141 total, 52 thin omitted)", "degree": 71},
#  {"id": "karpathy_repos_graph_report_communities", "label": "Communities", "degree": 54},
#  {"id": "graphify_skill_what_you_must_do_when_invoked", "label": "What You Must Do When Invoked", "degree": 16},
#  {"id": "graphify_skill_copilot_graphify", "label": "/graphify", "degree": 15}]
```

### `surprising_connections(...)` and `suggest_questions(...)`

Both lean on **communities** (clusters from `cluster.py`). `surprising_connections`
finds edges that jump between clusters or across languages (`_cross_language`,
`_cross_community_surprises`); `suggest_questions` proposes questions the graph
is uniquely positioned to answer (from AMBIGUOUS edges, bridge nodes,
underexplored god nodes, isolated nodes). **Signature gotcha:**

```python
suggest_questions(G, communities, community_labels, top_n=7)   # 3 required args
```

On the current AST-only graph these return little because **there are 0
communities** — they come alive only on a semantic/clustered build.

### `graph_diff(G_old, G_new)` — structural diff of two graphs

Compares two graphs and reports what changed:

```python
{
  "new_nodes":    [{"id": ..., "label": ...}],
  "removed_nodes":[{"id": ..., "label": ...}],
  "new_edges":    [{"source": ..., "target": ..., "relation": ..., "confidence": ...}],
  "removed_edges":[...],
  "summary": "3 new nodes, 5 new edges, 1 node removed"
}
```

> **Overlap to reconcile (still open).** This duplicates the structural-diff
> logic in your `snapshot.py` (`snapshot diff <sha-a> <sha-b>`). They should
> share one implementation — `snapshot diff` could call `analyze.graph_diff`.
> See [GIT_REVIEW_GUIDE.md](GIT_REVIEW_GUIDE.md) for the snapshot side.

`find_import_cycles(G)` rounds out the module — circular-dependency detection at
file level.

---

## 3. `report.py` — `generate(...)` renders the Markdown report

`report.generate()` is pure presentation: it takes the *outputs* of the analyze
functions and stitches them into the human-readable report. It does no graph
traversal of its own — note how its signature is a list of already-computed
analysis results:

```python
generate(G, communities, cohesion_scores, community_labels,
         god_node_list, surprise_list, detection_result, token_cost, root,
         suggested_questions=None, min_community_size=3, built_at_commit=None)
```

One useful detail: it computes the **confidence mix** of the edges
(`EXTRACTED` / `INFERRED` / `AMBIGUOUS` percentages) directly from
`G.edges(data=True)`, so the report tells you how much of the graph is hard fact
vs. LLM inference. It also normalizes string community keys back to int (JSON
round-trips turn int keys into strings — a recurring footgun across this
codebase).

---

## 4. `export.py` — serialize to other formats

`export.py` is the "write the graph somewhere" module. The graph is the single
source; each `to_*` is a projection:

- `to_json(G, communities, output_path, …)` — the canonical `graph.json`. Note
  it reloads as a **MultiDiGraph**, which is exactly why `diff.py` needs
  `_edge_relations()` (see [GIT_REVIEW_GUIDE.md](GIT_REVIEW_GUIDE.md)).
- `to_html(...)` / `_html_script(...)` — standalone interactive force-directed
  view (node/edge limits via `_viz_node_limit`).
- `to_cypher(...)` — Neo4j import statements; `push_to_neo4j(...)` writes live.
- `to_obsidian(...)` / `to_canvas(...)` — Markdown vault / Canvas board.
- `to_graphml(...)`, `to_svg(...)` — interchange + static image.
- `prune_dangling_edges(...)` — drops edges whose endpoints aren't in the node
  set (defensive cleanup before serialization); `backup_if_protected(...)`
  avoids clobbering a graph that was hand-edited.

---

## Why this graph looks sparse — and what changes with a semantic build

Everything above ran on an **AST-only** graph: structural `contains` edges
dominate, behavioral edges (`calls`, `inherits`) are rare, and clustering
produced **0 communities**. That is correct for an AST build, and it's why
`surprising_connections` / `suggest_questions` return little and why query seeds
land on doc headings.

Run the semantic pass (an LLM backend configured via `llm.py`) and the same
functions get much richer: communities appear, `surprising_connections` has
cross-cluster edges to surface, and the query path's seeds land on real code
entities. The consumption code is identical — only the graph underneath changes.

---

### See also
- [PIPELINE_GUIDE.md](PIPELINE_GUIDE.md) — the build side (`detect → extract → build → cluster`).
- [GIT_REVIEW_GUIDE.md](GIT_REVIEW_GUIDE.md) — `diff.py` + `snapshot.py` (Eugene's git-change-review extension).
- [CODE_GUIDE.md](CODE_GUIDE.md) — all 36 modules at a glance.
- [DATA_FLOW.md](DATA_FLOW.md) — concrete data-level trace with real JSON.
