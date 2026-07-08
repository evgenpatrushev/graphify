# Query-Quality Benchmark

Measures **how good graphify is at querying critical information** from a built
knowledge graph — across 3 real external codebases of increasing complexity.

Unlike `graphify/benchmark.py` (which measures only token *reduction*), this
benchmark measures answer *quality*: given a question, does graphify's query
surface the critical nodes/relationships a correct answer needs?

## Layout

```
benchmarks/
  build_corpora.py    # fetch click/flask/fastapi @ pinned SHAs, AST-build a graph for each
  query_quality.py    # score graphify's query results against the gold datasets
  judge.py            # optional LLM-judge (rates whether the answer is sufficient)
  METRICS.md          # reference for every metric (formulas, edge cases)
  datasets/           # gold question→expected-answer sets (one YAML per tier)
    simple.yaml       # pallets/click
    medium.yaml       # pallets/flask
    hard.yaml         # tiangolo/fastapi
  corpora/            # gitignored: fetched source trees (pinned SHA)
  out/                # gitignored: built graphs (<tier>/graph.json) + report.{md,json}
```

## Tiers

| Tier | Repo | Why |
|---|---|---|
| simple | `pallets/click` | small, single-package CLI lib — clear entrypoints |
| medium | `pallets/flask` | web framework — routing, app/blueprint, data flow |
| hard | `tiangolo/fastapi` | larger — dependency injection, async, many modules |

Repos are pinned to a commit SHA recorded in each tier's `datasets/*.yaml`
(`codebase.build.commit`) and in `out/<tier>/corpus_meta.json`.

## Build mode

**AST-only** (tree-sitter, no LLM): deterministic, free, offline after the
initial fetch. The graph is produced via the same pipeline as
`tests/test_pipeline.py`: `detect → extract → build_from_json → cluster → to_json`.

## Run

```bash
# 1. Fetch the repos and build a graph for each tier (network needed once)
python3 benchmarks/build_corpora.py                 # all tiers
python3 benchmarks/build_corpora.py --tier simple   # one tier

# 2. Score — deterministic metrics only (no API key, reproducible)
python3 benchmarks/query_quality.py                 # all tiers -> out/report.{md,json}
python3 benchmarks/query_quality.py --codebase simple --format md

# 3. Optional: add the LLM-judge pass (needs a configured backend, e.g. ANTHROPIC_API_KEY)
python3 benchmarks/query_quality.py --judge

# CI regression guard (simple tier, deterministic only)
pytest tests/test_query_benchmark.py
```

## Metrics

See [METRICS.md](METRICS.md) for full definitions. Headline numbers:

- **node_recall@budget** — fraction of required gold nodes that appear in the answer.
- **seed_hit@3** — fraction of expected seed nodes ranked in `_score_nodes`' top 3
  (mirrors `_pick_seeds(max_k=3)`, so it predicts whether traversal even starts in
  the right place).
- **judge_pass_rate** — (optional) fraction of answers the LLM-judge deems sufficient.

## Dataset schema

```yaml
codebase:
  id: simple                      # simple|medium|hard
  name: "pallets/click"
  build:
    repo_url: "https://github.com/pallets/click"
    commit: "<pinned-sha>"
    subdir: "src/click"
    mode: ast
defaults: { mode: bfs, depth: 3, token_budget: 2000, context_filters: null }
questions:
  - id: simple-entry-01
    question: "what is the main entry point for the CLI"
    category: entrypoint          # entrypoint|data-flow|error-handling|dependency|architecture|api|config
    difficulty: easy              # easy|medium|hard
    # optional per-question overrides: mode/depth/token_budget/context_filters
    expected:
      nodes:                      # gold critical nodes
        - { label: "BaseCommand", source_file: "core.py", required: true }
      relations:                  # OPTIONAL expected edges
        - { source: "Command", target: "invoke", relation: "calls", required: false }
      seed_expected:              # OPTIONAL nodes that SHOULD rank top in _score_nodes
        - { label: "BaseCommand", source_file: "core.py" }
```

- `required: true` nodes form the recall denominator; `required: false` = bonus only.
- Gold labels/`source_file`s are authored by building the graph first and reading
  `out/<tier>/graph.json` so they match exactly.

## Baseline numbers

Deterministic, AST-only build (no LLM-judge). Recorded 2026-06-15 at the pinned
SHAs above (click 1006n/3669e, flask 899n/2411e, fastapi 783n/3146e). 51 questions total.

| Tier | n | node_recall | seed_hit@3 | seed_mrr | token_reduction |
|---|---|---|---|---|---|
| simple (click) | 15 | 0.633 | 0.533 | 0.461 | 122.5× |
| medium (flask) | 17 | 0.397 | 0.265 | 0.278 | 279.5× |
| hard (fastapi) | 19 | 0.581 | 0.561 | 0.530 | 252.6× |
| **overall (macro)** | 51 | **0.537** | **0.453** | 0.423 | 218.2× |

`judge_pass_rate`: n/a (no LLM backend configured at baseline time).

**Reading the baseline.** node_recall ~0.54 means roughly half the critical
nodes survive into a 2000-token answer; the bottleneck is seeding, not
traversal — `seed_hit@3` ~0.45 shows the ranker often fails to put the right
node in the top 3, after which BFS can't reach it. The weakest cells are
**flask's method-call data-flow questions** (`.wsgi_app()`, `.dispatch_request()`,
context push/pop) which score ~0 recall: generic type-var nodes (`F`, `T`) and
common short labels dominate `_score_nodes`, burying the real entities. That is
the benchmark's headline finding and the most actionable lever for improving
graphify's query quality. `config`/`api`/`security` questions score highest
(distinctive, rare labels seed cleanly).
