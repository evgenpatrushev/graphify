"""Build AST-only knowledge graphs for the query-quality benchmark corpora.

Three external Python repositories are cloned at pinned SHAs and processed
through the full graphify pipeline (detect → extract → build → cluster →
export) with no LLM calls.  Outputs land in ``benchmarks/out/<tier>/``.

Usage::

    python3 benchmarks/build_corpora.py                   # build all tiers
    python3 benchmarks/build_corpora.py --tier simple
    python3 benchmarks/build_corpora.py --tier medium --force-refetch
    python3 benchmarks/build_corpora.py --tier hard   --force-refetch
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# sys.path bootstrap — mirrors tests/bench_extract.py so that ``graphify``
# is importable when this file is executed as a plain script from repo root.
# ---------------------------------------------------------------------------
_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Corpus definitions — edit commit fields once you have resolved SHAs.
# If commit is None the script clones the default branch and prints the
# resolved HEAD SHA so you can pin it here for reproducibility.
# ---------------------------------------------------------------------------
TIERS: list[dict[str, Any]] = [
    {
        "tier": "simple",
        "repo_url": "https://github.com/pallets/click",
        "subdir": "src/click",
        "commit": "8a1b1a33d739be05b7e91251e3c0dde77c5e152f",
    },
    {
        "tier": "medium",
        "repo_url": "https://github.com/pallets/flask",
        "subdir": "src/flask",
        "commit": "36e4a824f340fdee7ed50937ba8e7f6bc7d17f81",
    },
    {
        "tier": "hard",
        "repo_url": "https://github.com/tiangolo/fastapi",
        "subdir": "fastapi",
        "commit": "a4bd128ed50b4955a41cf9785b404312c4689ab3",
    },
]

# Derived paths (relative to project root, computed at import time)
_CORPORA_ROOT: Path = _PROJECT_ROOT / "benchmarks" / "corpora"
_OUT_ROOT: Path = _PROJECT_ROOT / "benchmarks" / "out"


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command, raising on non-zero exit."""
    return subprocess.run(
        cmd,
        check=True,
        text=True,
        capture_output=True,
        **kwargs,
    )


def _resolve_head(repo_dir: Path) -> str:
    """Return the current HEAD SHA of *repo_dir*."""
    result = _run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"])
    return result.stdout.strip()


def fetch_repo(tier_cfg: dict[str, Any], *, force_refetch: bool = False) -> str:
    """Clone or verify the repository described by *tier_cfg*.

    Returns the resolved commit SHA that was checked out.

    If ``tier_cfg["commit"]`` is ``None`` the default branch is cloned
    (shallow) and the HEAD SHA is printed so the caller can pin it.

    If *force_refetch* is ``True`` any existing clone is deleted first.
    """
    tier: str = tier_cfg["tier"]
    repo_url: str = tier_cfg["repo_url"]
    commit: str | None = tier_cfg["commit"]

    clone_dir: Path = _CORPORA_ROOT / tier
    clone_dir.parent.mkdir(parents=True, exist_ok=True)

    # Optionally wipe existing clone
    if force_refetch and clone_dir.exists():
        print(f"[{tier}] force-refetch: removing {clone_dir}")
        shutil.rmtree(clone_dir)

    # Skip clone when dir is already populated
    if clone_dir.exists() and any(clone_dir.iterdir()):
        resolved = _resolve_head(clone_dir)
        print(f"[{tier}] reusing existing clone at {clone_dir} (commit {resolved})")
        return resolved

    clone_dir.mkdir(parents=True, exist_ok=True)

    if commit is None:
        # Shallow clone of default branch; resolve and print HEAD
        print(f"[{tier}] cloning {repo_url} (default branch, shallow) …")
        _run(["git", "clone", "--depth", "1", repo_url, str(clone_dir)])
        resolved = _resolve_head(clone_dir)
        print(
            f"[{tier}] resolved HEAD SHA: {resolved}\n"
            f"         ↳ Pin this in TIERS['{tier}']['commit'] = '{resolved}' for reproducibility."
        )
    else:
        # Fetch a single specific commit (works on GitHub)
        print(f"[{tier}] fetching pinned commit {commit} from {repo_url} …")
        _run(["git", "-C", str(clone_dir), "init"])
        _run(["git", "-C", str(clone_dir), "remote", "add", "origin", repo_url])
        _run(["git", "-C", str(clone_dir), "fetch", "--depth", "1", "origin", commit])
        _run(["git", "-C", str(clone_dir), "checkout", "FETCH_HEAD"])
        resolved = _resolve_head(clone_dir)

    return resolved


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def build_graph(tier_cfg: dict[str, Any], *, force_refetch: bool = False) -> dict[str, Any]:
    """Fetch the repo and build a knowledge graph for *tier_cfg*.

    Returns the corpus metadata dict that is also written to
    ``benchmarks/out/<tier>/corpus_meta.json``.
    """
    # Late imports so the module is importable even before graphify is on
    # sys.path (e.g. during collection-only phases).
    from graphify.detect import detect
    from graphify.extract import extract
    from graphify.build import build_from_json
    from graphify.cluster import cluster
    from graphify.export import to_json

    tier: str = tier_cfg["tier"]
    repo_url: str = tier_cfg["repo_url"]
    subdir: str = tier_cfg["subdir"]

    # 1. Fetch
    resolved_sha = fetch_repo(tier_cfg, force_refetch=force_refetch)

    clone_dir: Path = _CORPORA_ROOT / tier
    corpus_subdir: Path = clone_dir / subdir
    if not corpus_subdir.exists():
        raise FileNotFoundError(
            f"[{tier}] subdir '{subdir}' not found in clone at {clone_dir}. "
            "Check the 'subdir' field in TIERS."
        )

    out_dir: Path = _OUT_ROOT / tier
    out_dir.mkdir(parents=True, exist_ok=True)

    # 2. Detect
    print(f"[{tier}] running detect on {corpus_subdir} …")
    det = detect(corpus_subdir)

    # 3. Extract (AST-only, no LLM)
    code_files = [Path(f) for f in det["files"].get("code", [])]
    print(f"[{tier}] extracting {len(code_files):,} code files …")
    extraction = extract(code_files)

    # 4. Build graph
    print(f"[{tier}] building graph …")
    G = build_from_json(extraction)

    # 5. Cluster
    print(f"[{tier}] clustering …")
    communities = cluster(G)

    # 6. Export graph.json (force=True to allow re-runs)
    graph_path = out_dir / "graph.json"
    print(f"[{tier}] exporting graph to {graph_path} …")
    to_json(G, communities, str(graph_path), force=True)

    # 7. Write corpus_meta.json
    meta: dict[str, Any] = {
        "tier": tier,
        "repo_url": repo_url,
        "commit": resolved_sha,
        "subdir": subdir,
        "total_words": det.get("total_words", 0),
        "total_files": det.get("total_files"),
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
    }
    meta_path = out_dir / "corpus_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # 8. Summary line
    print(
        f"[{tier}] DONE — nodes={G.number_of_nodes():,}  "
        f"edges={G.number_of_edges():,}  "
        f"total_words={det.get('total_words', 0):,}"
    )

    return meta


def build_all(*, force_refetch: bool = False) -> list[dict[str, Any]]:
    """Build graphs for all three tiers.  Returns a list of corpus-meta dicts."""
    results: list[dict[str, Any]] = []
    for tier_cfg in TIERS:
        results.append(build_graph(tier_cfg, force_refetch=force_refetch))
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry-point for ``python3 benchmarks/build_corpora.py``."""
    parser = argparse.ArgumentParser(
        description="Fetch external repos and build AST-only knowledge graphs for benchmarking.",
    )
    parser.add_argument(
        "--tier",
        choices=[t["tier"] for t in TIERS],
        default=None,
        help="Build a single tier instead of all three.",
    )
    parser.add_argument(
        "--force-refetch",
        action="store_true",
        help="Delete existing clones before fetching (forces a clean re-clone).",
    )
    args = parser.parse_args()

    if args.tier is not None:
        cfg = next(t for t in TIERS if t["tier"] == args.tier)
        build_graph(cfg, force_refetch=args.force_refetch)
    else:
        build_all(force_refetch=args.force_refetch)


if __name__ == "__main__":
    main()
