"""Optional LLM-judge for the graphify query-quality benchmark.

Provides :func:`judge_question` to score how well a graphify answer addresses
a benchmark question, and :func:`judge_available` to probe whether any backend
is reachable before committing to judged runs.

The module is intentionally dependency-light: only the Python standard library
(``json``, ``re``, ``sys``, ``pathlib``) plus the project-internal
``graphify.llm`` package are imported.

Typical usage::

    from benchmarks.judge import judge_available, judge_question

    if judge_available():
        result = judge_question(question, answer_text)
        print(result["score"], result["answers_question"])
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import TypedDict

# ---------------------------------------------------------------------------
# sys.path bootstrap — makes ``graphify`` importable when this file is run or
# imported from anywhere inside the repo tree, without requiring an editable
# install.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from graphify.llm import _call_llm, _get_backend_api_key, detect_backend  # noqa: E402


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class JudgeResult(TypedDict):
    """Shape of the dict returned by :func:`judge_question`."""

    available: bool
    """False when no backend is configured and the judge was skipped."""

    answers_question: bool | None
    """True if the answer text substantively addresses the question."""

    score: int | None
    """0-5 integer quality score (0 = useless, 5 = fully answers)."""

    missing: str | None
    """Short description of what critical information is absent, if any."""

    reason: str | None
    """Human-readable explanation for a skip or parse failure; None on success."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_CODE_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)
_BRACE_BLOCK_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)

_JUDGE_PROMPT_TEMPLATE = """\
You are a strict, impartial evaluator assessing whether a knowledge-graph \
answer adequately addresses a given question.

QUESTION:
{question}

ANSWER (node/edge subgraph returned by graphify):
{answer_text}

Evaluate the answer and reply with ONLY a single JSON object — no prose, \
no markdown, no code fences, nothing else. The object must have exactly \
these three keys:

  "answers_question": true or false — does the answer substantively address \
the question?
  "score": an integer from 0 to 5 — 0 means completely useless, 5 means \
the answer fully and clearly answers the question with no gaps.
  "missing": a short string (≤ 20 words) describing the most critical \
information absent from the answer, or an empty string if nothing is missing.

JSON output only. Do not include any text outside the JSON object.
"""


def _resolve_backend(backend: str | None) -> tuple[str | None, str | None]:
    """Return ``(backend_name, skip_reason)`` — skip_reason is set on failure.

    Never raises.
    """
    if backend is not None:
        # Caller supplied an explicit backend; trust it, but check for a key.
        try:
            key = _get_backend_api_key(backend)
            if not key and backend not in ("bedrock", "claude-cli", "ollama"):
                return None, f"no API key for explicit backend '{backend}'"
        except Exception as exc:  # noqa: BLE001
            return None, f"error checking API key for '{backend}': {exc}"
        return backend, None

    # Auto-detect.
    try:
        detected = detect_backend()
    except Exception as exc:  # noqa: BLE001
        return None, f"detect_backend() raised: {exc}"

    if not detected:
        return None, "no LLM backend configured (no API key found in environment)"

    return detected, None


def _strip_fences(text: str) -> str:
    """Remove surrounding code fences from a model response."""
    return _CODE_FENCE_RE.sub("", text).strip()


def _extract_json(text: str) -> dict | None:
    """Find and parse the first ``{...}`` block in *text*; return None on failure."""
    cleaned = _strip_fences(text)
    # Try the whole cleaned string first (happy path).
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fall back to the first {...} block (handles leading/trailing prose).
    match = _BRACE_BLOCK_RE.search(cleaned)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


def _make_skip(reason: str) -> JudgeResult:
    return JudgeResult(
        available=False,
        answers_question=None,
        score=None,
        missing=None,
        reason=reason,
    )


def _make_parse_error(snippet: str) -> JudgeResult:
    return JudgeResult(
        available=True,
        answers_question=None,
        score=None,
        missing=None,
        reason=f"unparseable: {snippet}",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def judge_available() -> bool:
    """Return ``True`` if a usable LLM backend appears to be configured.

    Best-effort, never raises.  Does *not* make any network call.
    """
    try:
        detected, _ = _resolve_backend(None)
        return detected is not None
    except Exception:  # noqa: BLE001
        return False


def judge_question(
    question: str,
    answer_text: str,
    *,
    backend: str | None = None,
    max_tokens: int = 400,
) -> JudgeResult:
    """Score how well *answer_text* addresses *question* using an LLM judge.

    Parameters
    ----------
    question:
        The benchmark question (plain English).
    answer_text:
        The NODE/EDGE subgraph block that graphify returned for the question.
    backend:
        Explicit backend name (e.g. ``"claude"``, ``"openai"``).  When
        ``None`` (the default) the backend is resolved via
        :func:`graphify.llm.detect_backend`.
    max_tokens:
        Upper bound on tokens in the model's reply.  400 is more than enough
        for a compact JSON object.

    Returns
    -------
    JudgeResult
        A dict with keys ``available``, ``answers_question``, ``score``,
        ``missing``, and ``reason``.  When no backend is reachable,
        ``available`` is ``False`` and all judgment fields are ``None``.
        On parse failure, ``available`` is ``True`` but the judgment fields
        are ``None`` and ``reason`` contains a diagnostic snippet.
    """
    resolved_backend, skip_reason = _resolve_backend(backend)
    if resolved_backend is None:
        return _make_skip(skip_reason or "unknown skip reason")

    prompt = _JUDGE_PROMPT_TEMPLATE.format(
        question=question.strip(),
        answer_text=answer_text.strip(),
    )

    try:
        raw_reply: str = _call_llm(prompt, backend=resolved_backend, max_tokens=max_tokens)
    except Exception as exc:  # noqa: BLE001
        return _make_skip(f"_call_llm raised: {exc}")

    parsed = _extract_json(raw_reply)
    if parsed is None:
        snippet = raw_reply[:120].replace("\n", " ")
        return _make_parse_error(snippet)

    # Extract and normalise fields; be tolerant of unexpected shapes.
    answers_question = parsed.get("answers_question")
    if isinstance(answers_question, str):
        answers_question = answers_question.lower() in ("true", "yes", "1")
    else:
        answers_question = bool(answers_question) if answers_question is not None else None

    raw_score = parsed.get("score")
    try:
        score: int | None = max(0, min(5, int(raw_score)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        score = None

    missing = parsed.get("missing")
    if not isinstance(missing, str):
        missing = str(missing) if missing is not None else ""

    return JudgeResult(
        available=True,
        answers_question=answers_question,
        score=score,
        missing=missing,
        reason=None,
    )
