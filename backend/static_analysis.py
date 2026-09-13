"""
Hybrid Static Analysis Layer
=============================

Implements improvement #4.2 from the AutoMaintainer Extension Report:
"Hybrid Static Analysis + LLM Review (not just AST navigation)".

Runs a real, deterministic static analyzer over submitted code *before*
the LLM ever sees it, so the Maintainer's narrative is grounded in actual
tool output (line numbers, rule codes) instead of prose the model
generated from pattern-matching alone.

Scope, stated honestly: a genuinely reliable static analyzer per language
is a large undertaking on its own (that's why standalone products like
SonarQube exist). This module ships one fully-working analyzer — Python,
via `ruff` — because Ruff is a single self-contained binary (no Node/JVM/
Go toolchain to install alongside it), covers correctness, style, and a
meaningful slice of security rules (flake8-bandit equivalents), and runs
in milliseconds.

For every other language, `run_static_analysis` returns an empty finding
list with `tool_available=False` rather than pretending to have analyzed
something it didn't — the review still proceeds using AST structure +
LLM reasoning + historical-rule grounding for those languages, just
without the deterministic layer. Adding a real analyzer for another
language means writing one function with this same signature and
registering it in STATIC_ANALYZERS — the interface doesn't change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Map ruff's rule-code prefix to one of the five rubric dimensions used by
# scoring.py, so a tool finding can be weighted correctly without the LLM
# having to re-classify it.
_RUFF_PREFIX_TO_DIMENSION = {
    "S": "security",       # flake8-bandit
    "E": "maintainability",  # pycodestyle errors
    "W": "maintainability",  # pycodestyle warnings
    "F": "correctness",    # pyflakes (undefined names, unused imports, etc.)
    "B": "correctness",    # flake8-bugbear (likely bugs)
    "C4": "maintainability",  # comprehensions
    "SIM": "maintainability",  # simplification
    "PERF": "performance",
    "N": "maintainability",  # naming
    "UP": "maintainability",  # pyupgrade
}


@dataclass
class StaticFinding:
    dimension: str  # correctness | security | performance | maintainability | tests
    severity: str  # low | medium | high
    line: Optional[int]
    message: str
    rule_code: Optional[str] = None
    source: str = "static-analyzer"


def _dimension_for_ruff_code(code: str) -> str:
    for prefix, dim in _RUFF_PREFIX_TO_DIMENSION.items():
        if code.startswith(prefix):
            return dim
    return "maintainability"


def _severity_for_ruff_code(code: str) -> str:
    # Security and correctness findings are weighted more severely by
    # default; everything else defaults to medium.
    if code.startswith("S"):
        return "high"
    if code.startswith(("F", "B")):
        return "medium"
    return "low"


async def _run_ruff(code: str, timeout: int = 15) -> tuple[list[StaticFinding], bool]:
    """Run ruff against a snippet via a temp file and parse its JSON output."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_file = Path(tmp_dir) / "submission.py"
        tmp_file.write_text(code, encoding="utf-8")

        try:
            proc = await asyncio.create_subprocess_exec(
                "ruff",
                "check",
                "--output-format=json",
                "--select=E,W,F,B,S,C4,SIM,PERF,N,UP",
                str(tmp_file),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except FileNotFoundError:
            logger.warning("ruff binary not found; skipping Python static analysis")
            return [], False
        except asyncio.TimeoutError:
            logger.warning("ruff timed out on submission")
            return [], True

        try:
            raw = stdout.decode("utf-8", errors="replace").strip()
            violations = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            logger.warning(
                f"ruff returned non-JSON output: {stderr.decode(errors='replace')[:200]}"
            )
            return [], True

        findings = [
            StaticFinding(
                dimension=_dimension_for_ruff_code(v.get("code", "")),
                severity=_severity_for_ruff_code(v.get("code", "")),
                line=v.get("location", {}).get("row"),
                message=v.get("message", ""),
                rule_code=v.get("code"),
            )
            for v in violations
        ]
        return findings, True


STATIC_ANALYZERS: dict[str, Callable] = {
    ".py": _run_ruff,
}


async def run_static_analysis(code: str, ext: str) -> tuple[list[StaticFinding], bool]:
    """
    Returns (findings, tool_available). tool_available=False tells the
    caller (review_engine.py) this language has no deterministic layer
    yet, so the prompt to the LLM should say so explicitly rather than
    silently implying "no findings" means "clean code".
    """
    analyzer = STATIC_ANALYZERS.get(ext)
    if analyzer is None:
        return [], False
    return await analyzer(code)
