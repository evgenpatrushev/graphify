# Graphify — Next Steps (session of 2026-06-05)

Autonomous evening run. Goal from you: review where we are, propose next steps,
and improve code understanding (readability + example-rich docs). Summary of what
got done and what I recommend next.

---

## What I did this run

1. **Built the graph on graphify itself and tested the new features end-to-end.**
   This was the outstanding blocker from last session. Found and fixed the build
   environment, then verified `diff`, `snapshot save/list` all run against a real
   1305→5525-node graph.

2. **Found and fixed a real bug in your `diff.py` ripple-impact.**
   `ripple_impact` reported **0 impacted nodes on every diff** because
   graphify's graph loads as a NetworkX *MultiGraph* and the code read edge
   relations with the simple-Graph shape. Added a `_edge_relations()` helper +
   regression tests (`tests/test_diff_ripple.py`, 6/6 passing). After the fix the
   `HEAD~3..HEAD` diff surfaces **1374 dependent nodes** instead of 0. This is the
   headline feature of your extension, so it's worth merging.

3. **Documentation for understanding:**
   - Corrected the wrong function-name table in `ARCHITECTURE.md` (it listed
     `collect_files`/`build_graph`/`analyze`/`render_report`/`export` — none of
     which exist; real names now listed and verified against source).
   - Added `docs/DATA_FLOW.md` — a concrete, data-level trace of the pipeline
     with real node/edge JSON and the three build "gotchas" I hit.
   - Cross-linked it from `docs/CODE_GUIDE.md` and fixed its stale pipeline line.

---

## Important findings you should know

- **Extraction fails silently without per-language grammar packages.** graphify
  imports `tree_sitter_python`, `tree_sitter_javascript`, etc. by name — *not*
  `tree-sitter-language-pack`. Missing one = that language contributes 0 nodes,
  but the build still says "success." My first build had **0 Python nodes** and
  looked fine. → *Recommend: make `build`/`update` print a loud warning that
  lists languages whose extractor errored, with a node-count-by-language summary.*

- **`graph_diff()` in `analyze.py` overlaps with your `snapshot.py`.** Both do
  graph-vs-graph structural diffs. Worth reconciling so there's one
  implementation (snapshot could call `analyze.graph_diff`).

- **AST-only builds are `contains`-heavy.** Without an LLM backend the graph is
  dominated by structural `contains` edges; behavioral relations (`calls`,
  `inherits`) are sparse unless you run the semantic pass.

---

## Recommended next steps, prioritized

**P0 — lock in the fix**
- Review & commit the `ripple_impact` MultiGraph fix + `tests/test_diff_ripple.py`.
- Run the full `pytest tests/` on your machine (the sandbox here has an unrelated
  tempfile-teardown quirk) to confirm nothing else regressed.

**P1 — make builds honest (small, high-value)**
- Add a per-language node-count summary + a warning when an extractor returns an
  `error`, so silent "0 Python nodes" builds can't happen again.

**P2 — finish the git-change-review vision**
- Wire `diff`/`snapshot` output into `graphify prs <number>` (per-PR graph impact)
  — `prs.py` already exposes the MCP hooks to hang this on.
- Add an LLM-based *intent summary* per changed community (using `llm.py`): turn
  the blast-radius node list into a 2–3 sentence "what this change is trying to do
  and what it might have missed." This is closest to your original goal of LLMs
  understanding intent with fewer tokens.
- Hook `snapshot save` into the git `post-commit` hook (`hooks.py`) so
  architectural-evolution snapshots accrue automatically.

**P3 — reconcile duplication**
- Make `snapshot diff` reuse `analyze.graph_diff()` (or vice versa) so there's one
  structural-diff implementation.

---

## How to pick up where this left off

```bash
pip install tree-sitter tree_sitter_python tree_sitter_javascript networkx
graphify update . --no-cluster
graphify diff HEAD~3 HEAD --depth 2     # now shows ripple impact
graphify snapshot save                  # then make a few commits and `snapshot diff`
```

New/changed files this session: `graphify/diff.py` (fix), `tests/test_diff_ripple.py`
(new), `ARCHITECTURE.md` (corrected), `docs/DATA_FLOW.md` (new),
`docs/CODE_GUIDE.md` (cross-link + pipeline fix).
