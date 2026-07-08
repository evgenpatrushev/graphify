"""Deterministic scoring harness for the graphify query-quality benchmark.

This module measures how well ``_query_graph_text`` surfaces the nodes and
edges that a gold dataset says should appear for each question.  It is
intentionally LLM-free; a separate ``benchmarks.judge`` module (written by
another agent) may be hooked in via ``--judge`` to add semantic scoring on
top of the deterministic metrics computed here.

Example NODE/EDGE lines produced by ``_subgraph_to_text`` (as tested by the
parser below):

    NODE MyClass [src=src/myclass.py loc=12 community=3]
    EDGE MyClass --inherits [EXTRACTED]--> BaseClass
    EDGE MyClass --calls [INFERRED context=call]--> helper()

The parser handles both forms:
- ``[CONF]``         — confidence only
- ``[CONF context=X]`` — confidence + context
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from statistics import harmonic_mean
from typing import Any

# ---------------------------------------------------------------------------
# sys.path bootstrap — works when run as a script from the repo root
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_BENCHMARKS_DIR = Path(__file__).resolve().parent
if str(_BENCHMARKS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_BENCHMARKS_DIR.parent))

import networkx as nx

# Lazy imports — these are only needed at runtime, not at import time, which
# keeps the module importable even before a graph has been built.
from graphify.serve import (  # noqa: E402
    _load_graph,
    _query_graph_text,
    _query_terms,
    _score_nodes,
    _strip_diacritics,
)
from graphify.benchmark import _estimate_tokens, _safe, _hr  # noqa: E402

try:
    import yaml  # type: ignore[import-untyped]
except ImportError as _yaml_err:
    raise ImportError("PyYAML is required: pip install pyyaml") from _yaml_err


# ---------------------------------------------------------------------------
# 1. Dataset loader
# ---------------------------------------------------------------------------

def load_dataset(path: str | Path) -> dict[str, Any]:
    """Load and validate a YAML benchmark dataset.

    Expected top-level keys: ``codebase``, ``defaults``, ``questions``.
    Returns the parsed dict verbatim; callers are expected to handle missing
    optional fields gracefully.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Dataset not found: {p}")
    with p.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Dataset {p} must be a YAML mapping at the top level.")
    for required_key in ("codebase", "questions"):
        if required_key not in data:
            raise ValueError(f"Dataset {p} is missing required key '{required_key}'.")
    return data


def effective_params(question: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Merge per-question overrides with dataset defaults.

    Keys: ``mode``, ``depth``, ``token_budget``, ``context_filters``.
    """
    base: dict[str, Any] = {
        "mode": "bfs",
        "depth": 3,
        "token_budget": 2000,
        "context_filters": None,
    }
    base.update({k: v for k, v in (defaults or {}).items() if v is not None})
    for key in ("mode", "depth", "token_budget", "context_filters"):
        if key in question and question[key] is not None:
            base[key] = question[key]
    return base


# ---------------------------------------------------------------------------
# 2. Output parser
# ---------------------------------------------------------------------------

# Exact line format from _subgraph_to_text:
#   NODE <label> [src=<src> loc=<loc> community=<community>]
#   EDGE <a> --<rel> [<conf>]--> <b>
#   EDGE <a> --<rel> [<conf> context=<ctx>]--> <b>
_NODE_RE = re.compile(
    r"^NODE (?P<label>.+?) \[src=(?P<src>.*?) loc=.*? community=.*?\]$"
)
_EDGE_RE = re.compile(
    r"^EDGE (?P<a>.+?) --(?P<rel>.*?) \[(?P<conf>[^\]]*)\]--> (?P<b>.+)$"
)


def parse_output(text: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Parse the text returned by ``_query_graph_text``.

    Returns:
        nodes: list of ``{label, src}`` dicts in order of appearance.
        edges: list of ``{a, rel, b, conf}`` dicts in order of appearance.

    The first non-empty line is the header (``Traversal: ... | ...``) and is
    intentionally skipped.  Lines that match neither NODE nor EDGE are ignored
    (e.g. the truncation notice).
    """
    nodes: list[dict[str, str]] = []
    edges: list[dict[str, str]] = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        m_node = _NODE_RE.match(line)
        if m_node:
            nodes.append({"label": m_node.group("label"), "src": m_node.group("src")})
            continue
        m_edge = _EDGE_RE.match(line)
        if m_edge:
            edges.append({
                "a": m_edge.group("a"),
                "rel": m_edge.group("rel"),
                "b": m_edge.group("b"),
                "conf": m_edge.group("conf").strip(),
            })
    return nodes, edges


# ---------------------------------------------------------------------------
# 3. Normalization + matching
# ---------------------------------------------------------------------------

def norm(x: str) -> str:
    """Normalize a label for fuzzy comparison.

    Applies diacritic stripping, lowercasing, trailing ``()`` removal, and
    whitespace collapsing — matching the logic used by ``_score_nodes``.
    """
    s = _strip_diacritics(x).lower()
    # strip trailing parentheses (e.g. "foo()" -> "foo")
    s = s.rstrip("()")
    # collapse internal whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def match_node(gold: dict[str, Any], returned: dict[str, str]) -> bool:
    """Return True if ``returned`` satisfies the gold node spec.

    Rules:
    - ``norm(gold.label)`` must be a substring of ``norm(returned.label)``
    - If ``gold.source_file`` is set, it must be a case-insensitive substring
      of ``returned.src``.
    """
    gold_norm = norm(gold.get("label", ""))
    ret_norm = norm(returned.get("label", ""))
    if gold_norm not in ret_norm:
        return False
    gold_src = gold.get("source_file")
    if gold_src:
        if gold_src.lower() not in returned.get("src", "").lower():
            return False
    return True


# ---------------------------------------------------------------------------
# 4. Deterministic metrics per question
# ---------------------------------------------------------------------------

def _match_endpoint(gold_label: str, returned_label: str) -> bool:
    """Endpoint substring match using norm()."""
    return norm(gold_label) in norm(returned_label)


def score_question(
    question_spec: dict[str, Any],
    returned_text: str,
    G: nx.Graph,
) -> dict[str, Any]:
    """Compute all deterministic metrics for a single question.

    Args:
        question_spec: The question dict from the dataset YAML.
        returned_text: The full string returned by ``_query_graph_text``.
        G: The NetworkX graph (used for seed scoring).

    Returns:
        A dict with keys: ``id``, ``question``, ``category``, ``difficulty``,
        ``node_recall``, ``node_precision``, ``node_f1``,
        ``edge_recall``, ``seed_mrr``, ``seed_hit@1``, ``seed_hit@3``,
        ``seed_hit@5``, ``token_reduction``, ``matched``, ``missed``,
        ``answer_tokens``.
    """
    qid = question_spec.get("id", "")
    question_text = question_spec.get("question", "")
    expected = question_spec.get("expected", {}) or {}

    ret_nodes, ret_edges = parse_output(returned_text)

    # ---- node recall / precision / F1 ----
    gold_nodes: list[dict[str, Any]] = expected.get("nodes", []) or []
    required_gold = [g for g in gold_nodes if g.get("required", True)]

    matched_required: list[dict[str, Any]] = []
    missed_required: list[dict[str, Any]] = []
    for g in required_gold:
        if any(match_node(g, r) for r in ret_nodes):
            matched_required.append(g)
        else:
            missed_required.append(g)

    if required_gold:
        node_recall: float | None = len(matched_required) / len(required_gold)
    else:
        node_recall = None

    if ret_nodes:
        # precision: fraction of returned nodes that match ANY gold node
        gold_all = gold_nodes  # optional nodes count toward precision satisfaction
        ret_matching = sum(
            1 for r in ret_nodes if any(match_node(g, r) for g in gold_all)
        )
        node_precision: float = ret_matching / len(ret_nodes)
    else:
        node_precision = 0.0

    if node_recall is not None and (node_recall + node_precision) > 0:
        node_f1: float | None = harmonic_mean([node_recall, node_precision])
    elif node_recall is None:
        node_f1 = None
    else:
        node_f1 = 0.0

    # ---- edge recall ----
    gold_relations: list[dict[str, Any]] = expected.get("relations", []) or []
    required_relations = [r for r in gold_relations if r.get("required", True)]
    edge_recall: float | None = None

    if required_relations:
        matched_edges = 0
        for gr in required_relations:
            g_src = gr.get("source", "")
            g_tgt = gr.get("target", "")
            g_rel = gr.get("relation")  # optional
            directed = gr.get("directed", False)

            found = False
            for re_edge in ret_edges:
                # forward match
                fwd = (
                    _match_endpoint(g_src, re_edge["a"])
                    and _match_endpoint(g_tgt, re_edge["b"])
                )
                # reverse match (only when undirected)
                rev = (not directed) and (
                    _match_endpoint(g_src, re_edge["b"])
                    and _match_endpoint(g_tgt, re_edge["a"])
                )
                if (fwd or rev) and (g_rel is None or norm(g_rel) in norm(re_edge["rel"])):
                    found = True
                    break
            if found:
                matched_edges += 1

        edge_recall = matched_edges / len(required_relations)

    # ---- seed MRR + hit@k ----
    seed_expected: list[dict[str, Any]] = expected.get("seed_expected", []) or []
    seed_mrr: float | None = None
    seed_hit1: float | None = None
    seed_hit3: float | None = None
    seed_hit5: float | None = None

    if seed_expected:
        terms = _query_terms(question_text)
        scored: list[tuple[float, str]] = _score_nodes(G, terms)

        reciprocal_ranks: list[float] = []
        hit1_list: list[float] = []
        hit3_list: list[float] = []
        hit5_list: list[float] = []

        for se in seed_expected:
            se_norm = norm(se.get("label", ""))
            se_src = se.get("source_file")
            best_rank: int | None = None
            for rank_0, (_, nid) in enumerate(scored):
                node_data = G.nodes[nid]
                node_label_norm = norm(node_data.get("label", nid))
                if se_norm not in node_label_norm:
                    continue
                if se_src and se_src.lower() not in (node_data.get("source_file") or "").lower():
                    continue
                best_rank = rank_0 + 1  # 1-indexed
                break

            if best_rank is not None:
                reciprocal_ranks.append(1.0 / best_rank)
                hit1_list.append(1.0 if best_rank <= 1 else 0.0)
                hit3_list.append(1.0 if best_rank <= 3 else 0.0)
                hit5_list.append(1.0 if best_rank <= 5 else 0.0)
            else:
                reciprocal_ranks.append(0.0)
                hit1_list.append(0.0)
                hit3_list.append(0.0)
                hit5_list.append(0.0)

        seed_mrr = sum(reciprocal_ranks) / len(reciprocal_ranks)
        seed_hit1 = sum(hit1_list) / len(hit1_list)
        seed_hit3 = sum(hit3_list) / len(hit3_list)
        seed_hit5 = sum(hit5_list) / len(hit5_list)

    # ---- token reduction ----
    answer_tokens = _estimate_tokens(returned_text)
    token_reduction: float | None = None  # filled later when corpus_meta available

    return {
        "id": qid,
        "question": question_text,
        "category": question_spec.get("category"),
        "difficulty": question_spec.get("difficulty"),
        "node_recall": node_recall,
        "node_precision": node_precision,
        "node_f1": node_f1,
        "edge_recall": edge_recall,
        "seed_mrr": seed_mrr,
        "seed_hit@1": seed_hit1,
        "seed_hit@3": seed_hit3,
        "seed_hit@5": seed_hit5,
        "token_reduction": token_reduction,
        "answer_tokens": answer_tokens,
        "matched": [g.get("label") for g in matched_required],
        "missed": [g.get("label") for g in missed_required],
        # Judge hook — filled externally when --judge is active
        "judge_score": None,
        "judge_pass": None,
    }


# ---------------------------------------------------------------------------
# 5. Aggregation
# ---------------------------------------------------------------------------

def _avg(values: list[float | None]) -> float | None:
    """Mean of non-None values; None if list is empty or all None."""
    valid = [v for v in values if v is not None]
    return sum(valid) / len(valid) if valid else None


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Macro-average a list of per-question result dicts."""
    return {
        "n": len(rows),
        "node_recall": _avg([r["node_recall"] for r in rows]),
        "node_precision": _avg([r["node_precision"] for r in rows]),
        "node_f1": _avg([r["node_f1"] for r in rows]),
        "edge_recall": _avg([r["edge_recall"] for r in rows]),
        "seed_mrr": _avg([r["seed_mrr"] for r in rows]),
        "seed_hit@1": _avg([r["seed_hit@1"] for r in rows]),
        "seed_hit@3": _avg([r["seed_hit@3"] for r in rows]),
        "seed_hit@5": _avg([r["seed_hit@5"] for r in rows]),
        "token_reduction": _avg([r["token_reduction"] for r in rows]),
        "judge_pass_rate": _avg([r["judge_pass"] for r in rows if r.get("judge_pass") is not None]) if any(r.get("judge_pass") is not None for r in rows) else None,
        "mean_judge_score": _avg([r["judge_score"] for r in rows if r.get("judge_score") is not None]) if any(r.get("judge_score") is not None for r in rows) else None,
    }


def build_report(
    per_question: list[dict[str, Any]],
    codebase_id: str,
) -> dict[str, Any]:
    """Build a structured report dict from per-question results.

    Shape::

        {
          "overall": {...},
          "per_codebase": {tier: {...}},
          "by_category": {...},
          "by_difficulty": {...},
          "per_question": [...],
        }
    """
    overall = _aggregate(per_question)

    # by_category
    categories: dict[str, list] = {}
    for r in per_question:
        cat = r.get("category") or "unknown"
        categories.setdefault(cat, []).append(r)
    by_category = {cat: _aggregate(rows) for cat, rows in categories.items()}

    # by_difficulty
    difficulties: dict[str, list] = {}
    for r in per_question:
        diff = r.get("difficulty") or "unknown"
        difficulties.setdefault(diff, []).append(r)
    by_difficulty = {diff: _aggregate(rows) for diff, rows in difficulties.items()}

    return {
        "overall": overall,
        "per_codebase": {codebase_id: overall},
        "by_category": by_category,
        "by_difficulty": by_difficulty,
        "per_question": per_question,
    }


def merge_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge per-codebase reports into a multi-codebase summary.

    Produces:
    - ``overall``: macro-average across codebases (equal weight per codebase).
    - ``per_codebase``: each codebase's aggregate.
    - ``by_category`` / ``by_difficulty``: pooled across all codebases.
    - ``per_question``: flat list of all per-question rows.
    - ``micro_average``: pooled average across all questions.
    """
    all_questions: list[dict[str, Any]] = []
    per_codebase: dict[str, Any] = {}
    for r in reports:
        all_questions.extend(r.get("per_question", []))
        per_codebase.update(r.get("per_codebase", {}))

    # macro: average of each codebase's aggregate
    codebase_aggs = list(per_codebase.values())
    macro_keys = [
        "node_recall", "node_precision", "node_f1", "edge_recall",
        "seed_mrr", "seed_hit@1", "seed_hit@3", "seed_hit@5",
        "token_reduction", "judge_pass_rate", "mean_judge_score",
    ]
    macro = {
        "n_codebases": len(codebase_aggs),
        "n_questions": len(all_questions),
    }
    for k in macro_keys:
        macro[k] = _avg([agg.get(k) for agg in codebase_aggs])

    micro = _aggregate(all_questions)

    # pooled breakdowns
    categories: dict[str, list] = {}
    difficulties: dict[str, list] = {}
    for r in all_questions:
        cat = r.get("category") or "unknown"
        categories.setdefault(cat, []).append(r)
        diff = r.get("difficulty") or "unknown"
        difficulties.setdefault(diff, []).append(r)
    by_category = {cat: _aggregate(rows) for cat, rows in categories.items()}
    by_difficulty = {diff: _aggregate(rows) for diff, rows in difficulties.items()}

    return {
        "overall": macro,
        "micro_average": micro,
        "per_codebase": per_codebase,
        "by_category": by_category,
        "by_difficulty": by_difficulty,
        "per_question": all_questions,
    }


# ---------------------------------------------------------------------------
# 6. Rendering
# ---------------------------------------------------------------------------

def _fmt(value: float | None, decimals: int = 3) -> str:
    """Format a nullable float for display."""
    if value is None:
        return "n/a"
    return f"{value:.{decimals}f}"


def render_json(report: dict[str, Any]) -> str:
    """Serialize the report to a pretty-printed JSON string."""
    return json.dumps(report, indent=2, default=str)


def render_markdown(report: dict[str, Any]) -> str:  # noqa: PLR0912
    """Render a human-readable Markdown summary of the report."""
    arrow = _safe("→", "->")
    hr = _hr(60)
    lines: list[str] = ["# graphify Query-Quality Benchmark\n", hr, ""]

    def _agg_table(title: str | None, agg: dict[str, Any]) -> None:
        if title is not None:
            lines.append(f"## {title}\n")
        pairs = [
            ("Questions", str(agg.get("n", agg.get("n_questions", "?")))),
            ("node_recall", _fmt(agg.get("node_recall"))),
            ("node_precision", _fmt(agg.get("node_precision"))),
            ("node_f1", _fmt(agg.get("node_f1"))),
            ("edge_recall", _fmt(agg.get("edge_recall"))),
            ("seed_mrr", _fmt(agg.get("seed_mrr"))),
            (f"seed_hit{_safe('@', 'at')}3", _fmt(agg.get("seed_hit@3"))),
            (f"seed_hit{_safe('@', 'at')}5", _fmt(agg.get("seed_hit@5"))),
            ("token_reduction", _fmt(agg.get("token_reduction"), 1)),
            ("judge_pass_rate", _fmt(agg.get("judge_pass_rate"))),
            ("mean_judge_score", _fmt(agg.get("mean_judge_score"))),
        ]
        col_w = max(len(k) for k, _ in pairs) + 2
        for k, v in pairs:
            lines.append(f"  {k:<{col_w}} {arrow}  {v}")
        lines.append("")

    _agg_table("Overall (macro-average across codebases)", report.get("overall", {}))

    if "micro_average" in report:
        _agg_table("Micro-average (pooled across all questions)", report["micro_average"])

    # Per-codebase
    per_cb = report.get("per_codebase", {})
    if per_cb:
        lines.append("## Per-Codebase\n")
        for cb_id, agg in per_cb.items():
            lines.append(f"### {cb_id}\n")
            _agg_table(None, agg)

    # By category
    by_cat = report.get("by_category", {})
    if by_cat:
        lines.append("## By Category\n")
        lines.append(f"{'Category':<20} {'n':>4} {'recall':>8} {'precision':>10} {'f1':>8} {'edge_rec':>9}")
        lines.append("-" * 63)
        for cat, agg in sorted(by_cat.items()):
            lines.append(
                f"{cat:<20} {agg.get('n', 0):>4} {_fmt(agg.get('node_recall')):>8}"
                f" {_fmt(agg.get('node_precision')):>10} {_fmt(agg.get('node_f1')):>8}"
                f" {_fmt(agg.get('edge_recall')):>9}"
            )
        lines.append("")

    # By difficulty
    by_diff = report.get("by_difficulty", {})
    if by_diff:
        lines.append("## By Difficulty\n")
        lines.append(f"{'Difficulty':<14} {'n':>4} {'recall':>8} {'precision':>10} {'f1':>8}")
        lines.append("-" * 48)
        for diff, agg in sorted(by_diff.items()):
            lines.append(
                f"{diff:<14} {agg.get('n', 0):>4} {_fmt(agg.get('node_recall')):>8}"
                f" {_fmt(agg.get('node_precision')):>10} {_fmt(agg.get('node_f1')):>8}"
            )
        lines.append("")

    # Per-question table
    per_q = report.get("per_question", [])
    if per_q:
        lines.append("## Per-Question Results\n")
        header = f"{'id':<20} {'cat':<12} {'diff':<8} {'rec':>6} {'prec':>6} {'f1':>6} {'e_rec':>6} {'mrr':>6} {'h@3':>5}"
        lines.append(header)
        lines.append("-" * len(header))
        for r in per_q:
            qid = str(r.get("id", ""))[:19]
            cat = str(r.get("category") or "")[:11]
            diff = str(r.get("difficulty") or "")[:7]
            lines.append(
                f"{qid:<20} {cat:<12} {diff:<8}"
                f" {_fmt(r.get('node_recall'), 3):>6}"
                f" {_fmt(r.get('node_precision'), 3):>6}"
                f" {_fmt(r.get('node_f1'), 3):>6}"
                f" {_fmt(r.get('edge_recall'), 3):>6}"
                f" {_fmt(r.get('seed_mrr'), 3):>6}"
                f" {_fmt(r.get('seed_hit@3'), 3):>5}"
            )
            if r.get("missed"):
                lines.append(f"  {'missed: ' + ', '.join(r['missed'])}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7. CLI main()
# ---------------------------------------------------------------------------

def _load_corpus_meta(graph_dir: Path) -> dict[str, Any] | None:
    """Load corpus_meta.json from the graph directory if it exists."""
    meta_path = graph_dir / "corpus_meta.json"
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _apply_token_reduction(
    per_question: list[dict[str, Any]],
    corpus_meta: dict[str, Any] | None,
) -> None:
    """Mutate per-question dicts to fill in ``token_reduction`` from corpus_meta."""
    if corpus_meta is None:
        return
    total_words = corpus_meta.get("total_words")
    if not total_words:
        return
    corpus_tokens = total_words * 100 // 75
    for r in per_question:
        qt = r.get("answer_tokens") or 0
        if qt > 0:
            r["token_reduction"] = round(corpus_tokens / qt, 2)


def run_dataset(
    dataset_path: Path,
    graph_dir: Path,
    judge: bool = False,
) -> dict[str, Any]:
    """Run the benchmark for a single dataset file.

    Args:
        dataset_path: Path to the YAML dataset.
        graph_dir: Directory containing ``graph.json`` (and optionally
            ``corpus_meta.json``) for this codebase tier.
        judge: Whether to call the LLM judge (requires ``benchmarks.judge``).

    Returns:
        A report dict as produced by ``build_report``.
    """
    dataset = load_dataset(dataset_path)
    codebase = dataset.get("codebase", {})
    codebase_id = codebase.get("id", dataset_path.stem)
    defaults = dataset.get("defaults", {}) or {}
    questions = dataset.get("questions", []) or []

    graph_path = graph_dir / "graph.json"
    G = _load_graph(str(graph_path))

    corpus_meta = _load_corpus_meta(graph_dir)

    # Optionally import the LLM judge (non-fatal)
    judge_fn = None
    if judge:
        try:
            from benchmarks.judge import judge_question as _judge_fn  # type: ignore[import-not-found]
            judge_fn = _judge_fn
        except ImportError:
            print(
                "warning: --judge requested but 'benchmarks.judge' could not be imported. "
                "Judge metrics will be null.",
                file=sys.stderr,
            )

    per_question: list[dict[str, Any]] = []
    for q_spec in questions:
        params = effective_params(q_spec, defaults)
        answer_text = _query_graph_text(
            G,
            q_spec.get("question", ""),
            mode=params["mode"],
            depth=params["depth"],
            token_budget=params["token_budget"],
            context_filters=params["context_filters"],
        )
        result = score_question(q_spec, answer_text, G)

        # Judge hook
        if judge_fn is not None:
            try:
                judge_result = judge_fn(q_spec.get("question", ""), answer_text)
                result["judge_score"] = judge_result.get("score")
                result["judge_pass"] = judge_result.get("answers_question")
            except Exception as exc:
                print(f"warning: judge failed for question {result['id']!r}: {exc}", file=sys.stderr)

        per_question.append(result)

    _apply_token_reduction(per_question, corpus_meta)

    return build_report(per_question, codebase_id)


def main() -> None:
    """Entry point: ``python3 benchmarks/query_quality.py [options]``."""
    parser = argparse.ArgumentParser(
        description="Graphify query-quality benchmark (deterministic scoring)."
    )
    parser.add_argument(
        "--codebase",
        nargs="+",
        metavar="TIER",
        default=None,
        help="Codebase tier(s) to benchmark (e.g. simple medium hard). "
             "Defaults to all YAML files found under --datasets-dir.",
    )
    parser.add_argument(
        "--format",
        choices=["md", "json", "both"],
        default="both",
        help="Output format (default: both).",
    )
    parser.add_argument(
        "--judge",
        action="store_true",
        default=False,
        help="Call benchmarks.judge.judge_question for LLM-based scoring.",
    )
    parser.add_argument(
        "--graph-dir",
        default="benchmarks/out",
        metavar="DIR",
        help="Root directory that contains <tier>/graph.json sub-directories.",
    )
    parser.add_argument(
        "--datasets-dir",
        default="benchmarks/datasets",
        metavar="DIR",
        help="Directory containing YAML dataset files.",
    )
    args = parser.parse_args()

    graph_root = Path(args.graph_dir)
    datasets_root = Path(args.datasets_dir)

    # Discover dataset files
    if not datasets_root.exists():
        print(f"error: datasets directory not found: {datasets_root}", file=sys.stderr)
        sys.exit(1)

    yaml_files = sorted(datasets_root.glob("*.yaml")) + sorted(datasets_root.glob("*.yml"))
    if not yaml_files:
        print(f"error: no YAML dataset files found in {datasets_root}", file=sys.stderr)
        sys.exit(1)

    # Filter by --codebase if requested
    if args.codebase:
        tiers = set(args.codebase)
        yaml_files = [f for f in yaml_files if f.stem in tiers]
        if not yaml_files:
            print(f"error: no datasets matched tiers {tiers}", file=sys.stderr)
            sys.exit(1)

    reports: list[dict[str, Any]] = []
    for yaml_path in yaml_files:
        tier = yaml_path.stem
        graph_dir = graph_root / tier
        if not (graph_dir / "graph.json").exists():
            print(
                f"warning: graph not found at {graph_dir / 'graph.json'} — skipping {tier}",
                file=sys.stderr,
            )
            continue
        print(f"Running benchmark for '{tier}'...")
        try:
            report = run_dataset(yaml_path, graph_dir, judge=args.judge)
            reports.append(report)
        except Exception as exc:
            print(f"error: benchmark failed for '{tier}': {exc}", file=sys.stderr)

    if not reports:
        print("No reports generated (check that graphs are built).", file=sys.stderr)
        sys.exit(1)

    final_report = merge_reports(reports) if len(reports) > 1 else reports[0]

    # Write outputs
    out_dir = graph_root
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.format in ("json", "both"):
        json_path = out_dir / "report.json"
        json_path.write_text(render_json(final_report), encoding="utf-8")
        print(f"Wrote {json_path}")

    if args.format in ("md", "both"):
        md_path = out_dir / "report.md"
        md_path.write_text(render_markdown(final_report), encoding="utf-8")
        print(f"Wrote {md_path}")

    # Print summary to stdout
    overall = final_report.get("overall", {})
    print(f"\n{_hr(60)}")
    print("Query-Quality Benchmark Summary")
    print(_hr(60))
    arrow = _safe("→", "->")
    for label, key in [
        ("node_recall", "node_recall"),
        ("node_precision", "node_precision"),
        ("node_f1", "node_f1"),
        ("edge_recall", "edge_recall"),
        ("seed_mrr", "seed_mrr"),
        ("seed_hit@3", "seed_hit@3"),
        ("token_reduction", "token_reduction"),
    ]:
        print(f"  {label:<18} {arrow}  {_fmt(overall.get(key))}")
    if overall.get("judge_pass_rate") is not None:
        print(f"  {'judge_pass_rate':<18} {arrow}  {_fmt(overall['judge_pass_rate'])}")
    print()


if __name__ == "__main__":
    main()
