# Query-Quality Benchmark — Metrics Reference

This document is the reference for every number the query-quality benchmark
produces. The harness (`query_quality.py`) computes these against gold datasets
(`datasets/*.yaml`) for graphs built by `build_corpora.py`.

Headline numbers: **mean node-recall@budget** and **seed hit@3** (deterministic),
plus **LLM-judge pass-rate** (optional, `--judge`).

---

## Definitions

For a single question `q` against graph `G`:

- `Gr` = set of **required** gold nodes (`required: true`); `Gall` = all gold nodes.
- `R` = ordered list of returned `NODE` lines from `_query_graph_text`.
- `Er` = set of **required** gold relations; `RE` = returned `EDGE` lines.
- `match(g, r)`: `norm(g.label) ⊆ norm(r.label)` AND (`g.source_file` unset OR `g.source_file.lower() ⊆ r.src.lower()`).
- `norm(x) = _strip_diacritics(x).lower()`, with a trailing `()` stripped and whitespace collapsed.

The harness uses graphify's own `serve._strip_diacritics` for normalization so
that matching is identical to how the engine scores labels.

---

## Deterministic metrics (per question)

| Metric | Formula | What it tells you |
|---|---|---|
| **node_recall@budget** | `|{g ∈ Gr : ∃ r ∈ R, match(g,r)}| / |Gr|` | Did the critical info survive into the budgeted answer? **(headline)** |
| **node_precision** | `|{r ∈ R : ∃ g ∈ Gall, match(g,r)}| / |R|` | How much of the answer is on-target (co-varies with budget; low is informative, not bad). |
| **node_f1** | `2·P·R / (P+R)` | Reported, not optimized. |
| **edge_recall** | `|matched Er| / |Er|` (only if `relations` set) | Are the critical *relationships* present? Endpoints matched by label-substring; relation matched if `relation` given. Undirected unless `directed: true`. |
| **seed_mrr** | `mean(1/k)` over `seed_expected`, `k` = best (lowest) 1-indexed rank in `_score_nodes` whose node matches; `1/k=0` if absent | Does the ranker aim at the right node before traversal? |
| **seed_hit@k** (k∈{1,3,5}) | fraction of `seed_expected` whose match is in top-k of `_score_nodes` | hit@3 mirrors `_pick_seeds(max_k=3)` → predicts traversal starts in the right place. **(headline)** |
| **token_reduction** | `corpus_tokens / query_tokens` | Efficiency. `corpus_tokens = total_words*100//75`; `query_tokens = _estimate_tokens(answer_text)`. |

---

## LLM-judge metrics (per question, optional `--judge`)

Prompt `llm._call_llm` with the question + the returned NODE/EDGE block, asking
for strict JSON `{answers_question: bool, score: int 0-5, missing: string}`.

- **judge_pass** = `answers_question` (bool)
- **judge_score** = 0–5
- Aggregate → **judge_pass_rate** and **mean_judge_score**.
- Non-deterministic; never gates CI; logged-skip when no backend configured.

---

## Aggregation levels

1. **Per question** — all metrics above.
2. **Per codebase** — macro-average each metric over its questions; plus
   breakdowns by `category` and by `difficulty`.
3. **Overall** — macro-average across the 3 codebases (equal weight each, so a
   large hard set can't dominate); micro-average (pooled questions) reported as
   a secondary figure.

---

## Edge cases / rules

- `|R| = 0` → recall 0, precision 0 (no divide-by-zero; precision defined as 0).
- `|Gr| = 0` (a question with only bonus nodes) → recall omitted from that
  question's recall average, flagged in the report.
- A returned node matching multiple gold nodes counts once per gold node for
  recall, once total for precision.
- Determinism: AST build + `cluster()` + `_score_nodes` are deterministic given
  pinned SHAs; deterministic metrics are byte-stable across runs. Only
  `--judge` introduces variance.

---

## Report shape (`out/report.json`)

```json
{
  "overall": {"node_recall": 0.0, "seed_hit@3": 0.0, "judge_pass_rate": null, "...": "..."},
  "per_codebase": {"simple": {"...": "..."}, "medium": {}, "hard": {}},
  "by_category": {"entrypoint": {"...": "..."}},
  "by_difficulty": {"easy": {"...": "..."}},
  "per_question": [{"id": "simple-entry-01", "node_recall": 1.0, "matched": ["..."], "missed": ["..."], "...": "..."}]
}
```

`out/report.md` renders the same data as human-readable tables (reuse `_safe`/`_hr`).
