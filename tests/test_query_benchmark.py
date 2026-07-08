"""Regression guards for the query-quality benchmark (benchmarks/).

These tests cover the parts that must stay correct regardless of network access:
  * every gold dataset parses and is structurally sane;
  * the NODE/EDGE output parser round-trips a known `_query_graph_text` block;
  * IF the simple-tier graph has already been built, the simple-tier mean
    node-recall stays above a conservative floor (catches scoring regressions);
  * IF the simple-tier corpus has already been cloned, rebuilding it is
    deterministic (stable node/edge counts).

The graph-dependent tests `skip` (not fail) when their inputs are absent, so the
suite is green on a fresh checkout with no `benchmarks/out/` or `benchmarks/corpora/`.
Run `python3 benchmarks/build_corpora.py --tier simple` first to exercise them.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks import query_quality as qq  # noqa: E402

DATASETS_DIR = REPO_ROOT / "benchmarks" / "datasets"
OUT_DIR = REPO_ROOT / "benchmarks" / "out"
CORPORA_DIR = REPO_ROOT / "benchmarks" / "corpora"

_VALID_CATEGORIES = {
    "entrypoint", "data-flow", "error-handling",
    "dependency", "architecture", "api", "config",
}
_VALID_DIFFICULTIES = {"easy", "medium", "hard"}

# Conservative floor: observed simple-tier macro recall is ~0.63; a real
# scoring/matching regression would drop well below this.
_SIMPLE_RECALL_FLOOR = 0.45


@pytest.mark.parametrize("name", ["simple", "medium", "hard"])
def test_dataset_parses_and_is_sane(name: str):
    path = DATASETS_DIR / f"{name}.yaml"
    assert path.exists(), f"missing dataset {path}"
    data = yaml.safe_load(path.read_text())

    assert data["codebase"]["id"] == name
    build = data["codebase"]["build"]
    assert build["repo_url"] and build["commit"] and build["subdir"]

    questions = data["questions"]
    assert len(questions) >= 10, f"{name}: expected a substantial question set"

    seen_ids = set()
    for q in questions:
        assert q["id"] not in seen_ids, f"duplicate question id {q['id']}"
        seen_ids.add(q["id"])
        assert q["question"].strip()
        assert q["category"] in _VALID_CATEGORIES, f"{q['id']}: bad category {q['category']}"
        assert q["difficulty"] in _VALID_DIFFICULTIES, f"{q['id']}: bad difficulty"
        nodes = q.get("expected", {}).get("nodes", [])
        assert nodes, f"{q['id']}: must declare at least one expected node"
        for n in nodes:
            assert n.get("label"), f"{q['id']}: expected node missing label"


def test_parser_roundtrips_node_and_edge_lines():
    text = (
        "Traversal: BFS depth=3 | Start: [foo] | 2 nodes found\n"
        "\n"
        "NODE invoke() [src=core.py loc=L42 community=3]\n"
        "NODE Command [src=core.py loc=L10 community=3]\n"
        "EDGE Command --calls [EXTRACTED]--> invoke()\n"
    )
    nodes, edges = qq.parse_output(text)

    labels = {n["label"] for n in nodes}
    assert "invoke()" in labels
    assert "Command" in labels
    srcs = {n["src"] for n in nodes}
    assert srcs == {"core.py"}

    assert len(edges) == 1
    e = edges[0]
    assert e["a"] == "Command"
    assert e["b"] == "invoke()"
    assert e["rel"] == "calls"


def test_match_node_substring_and_source_file():
    # gold "invoke" matches returned "invoke()" in core.py
    assert qq.match_node(
        {"label": "invoke", "source_file": "core.py"},
        {"label": "invoke()", "src": "core.py"},
    )
    # source_file constraint excludes a same-label node in another file
    assert not qq.match_node(
        {"label": "invoke", "source_file": "decorators.py"},
        {"label": "invoke()", "src": "core.py"},
    )


@pytest.mark.skipif(
    not (OUT_DIR / "simple" / "graph.json").exists(),
    reason="simple-tier graph not built; run benchmarks/build_corpora.py --tier simple",
)
def test_simple_tier_recall_above_floor():
    report = qq.run_dataset(DATASETS_DIR / "simple.yaml", OUT_DIR / "simple")
    recall = report["overall"]["node_recall"]
    assert recall is not None
    assert recall >= _SIMPLE_RECALL_FLOOR, (
        f"simple-tier node_recall {recall:.3f} fell below floor {_SIMPLE_RECALL_FLOOR}"
    )


@pytest.mark.skipif(
    not (CORPORA_DIR / "simple").exists(),
    reason="simple-tier corpus not cloned; run benchmarks/build_corpora.py --tier simple",
)
def test_simple_build_is_deterministic(tmp_path, monkeypatch):
    """Rebuilding the already-cloned simple corpus yields stable node/edge counts."""
    import json

    from benchmarks import build_corpora as bc

    meta_path = OUT_DIR / "simple" / "corpus_meta.json"
    if not meta_path.exists():
        pytest.skip("no baseline corpus_meta.json to compare against")
    baseline = json.loads(meta_path.read_text())

    # Build into a temp out-dir (redirect the module's output root so we never
    # clobber the committed baseline), reusing the existing clone (no network).
    monkeypatch.setattr(bc, "_OUT_ROOT", tmp_path, raising=True)
    tier_cfg = next(t for t in bc.TIERS if t["tier"] == "simple")
    result = bc.build_graph(tier_cfg)

    assert result["nodes"] == baseline["nodes"], "node count drifted (non-deterministic build)"
    assert result["edges"] == baseline["edges"], "edge count drifted (non-deterministic build)"
