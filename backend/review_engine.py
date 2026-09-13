"""
Standalone Review Engine
==========================

Implements improvement #4.1 (decoupled submission flow) together with
#4.2 (hybrid static+AI analysis), #4.3 (rubric scoring), and #4.4
(historical-rule grounding) from the AutoMaintainer Extension Report.

This module is deliberately independent of the existing 5-agent
LangGraph pipeline (agents.py) and the GitHub-PR-centric flow in main.py.
It is called by the new `run_code_review_task` Celery task (tasks.py) and
returns one structured `ReviewResult` — nothing here creates branches,
issues, or PRs.

Honesty note on what is and isn't tested: everything in this file up to
the actual LLM network call has been exercised against real code samples
in a sandbox during development (AST parsing, static analysis, rule
retrieval, scoring). The LLM call itself (`run_llm_with_rate_limit`) was
NOT live-tested end-to-end here because api.groq.com isn't reachable from
that sandbox — but `parse_llm_review_response`, the part most likely to
break silently (models don't always return clean JSON), IS unit-tested
against a hand-written sample response in the accompanying test file.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from ast_indexer import TreeSitterParser, detect_extension, EXTENSION_TO_LANGUAGE_NAME
from static_analysis import run_static_analysis, StaticFinding
from scoring import compute_rubric_score, detect_tests, RubricResult
from rules_engine import RulesIndex, select_rules_for_grounding, HistoricalRule

logger = logging.getLogger(__name__)

VALID_DIMENSIONS = {"correctness", "security", "performance", "maintainability"}
VALID_SEVERITIES = {"low", "medium", "high"}


@dataclass
class LLMFinding:
    """Shares the (dimension, severity) shape StaticFinding uses so both
    can be merged into one list for scoring.compute_rubric_score without
    the scorer needing to know which source produced which finding."""

    dimension: str
    severity: str
    line: Optional[int]
    message: str
    matched_rule_ref: Optional[str] = None
    source: str = "llm-review"


@dataclass
class ParsedLLMReview:
    summary: str
    findings: list[LLMFinding]
    architecture_notes: list[str]
    optimization_notes: list[str]
    parse_ok: bool
    raw_text: str = ""


@dataclass
class ReviewResult:
    language: str
    rubric: RubricResult
    summary: str
    findings: list  # merged StaticFinding + LLMFinding, for the report/UI
    architecture_notes: list[str]
    optimization_notes: list[str]
    matched_historical_rules: list[dict]
    static_analysis_available: bool
    llm_parse_ok: bool

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "score": self.rubric.overall_score,
            "rubric": self.rubric.to_dict(),
            "summary": self.summary,
            "findings": [
                {
                    "dimension": f.dimension,
                    "severity": f.severity,
                    "line": f.line,
                    "message": f.message,
                    "rule_code": getattr(f, "rule_code", None),
                    "matched_rule_ref": getattr(f, "matched_rule_ref", None),
                    "source": f.source,
                }
                for f in self.findings
            ],
            "architecture_notes": self.architecture_notes,
            "optimization_notes": self.optimization_notes,
            "matched_historical_rules": self.matched_historical_rules,
            "static_analysis_available": self.static_analysis_available,
            "llm_parse_ok": self.llm_parse_ok,
        }


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    fence_match = re.match(r"^```(?:json)?\s*(.*)```$", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    return text


def _extract_json_object(text: str) -> Optional[str]:
    """Fallback for when the model adds prose before/after the JSON:
    grab the outermost {...} block by brace counting."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_llm_review_response(raw_text: str) -> ParsedLLMReview:
    """
    Pure function, no I/O — this is what's unit-tested in
    test_review_engine.py against a hand-written sample response, since
    a real Groq call can't be made from a network-restricted sandbox.
    """
    candidate = _strip_code_fences(raw_text)

    parsed = None
    for attempt in (candidate, _extract_json_object(candidate)):
        if attempt is None:
            continue
        try:
            parsed = json.loads(attempt)
            break
        except json.JSONDecodeError:
            continue

    if parsed is None or not isinstance(parsed, dict):
        logger.warning("Could not parse LLM review response as JSON")
        return ParsedLLMReview(
            summary=raw_text.strip()[:2000],
            findings=[],
            architecture_notes=[],
            optimization_notes=[],
            parse_ok=False,
            raw_text=raw_text,
        )

    findings = []
    for item in parsed.get("findings", []) or []:
        if not isinstance(item, dict):
            continue
        dimension = str(item.get("dimension", "")).lower()
        severity = str(item.get("severity", "")).lower()
        if dimension not in VALID_DIMENSIONS or severity not in VALID_SEVERITIES:
            # Don't silently drop the whole response over one malformed
            # finding — skip just that finding and keep going.
            continue
        findings.append(
            LLMFinding(
                dimension=dimension,
                severity=severity,
                line=item.get("line"),
                message=str(item.get("message", "")).strip(),
                matched_rule_ref=item.get("matched_rule_ref"),
            )
        )

    return ParsedLLMReview(
        summary=str(parsed.get("summary", "")).strip(),
        findings=findings,
        architecture_notes=[str(n) for n in parsed.get("architecture_notes", []) or []],
        optimization_notes=[str(n) for n in parsed.get("optimization_notes", []) or []],
        parse_ok=True,
        raw_text=raw_text,
    )


def build_review_prompt(
    code: str,
    language_name: str,
    ast_summary: Optional[dict],
    static_findings: list[StaticFinding],
    static_tool_available: bool,
    grounded_rules: list[tuple[HistoricalRule, Optional[float]]],
) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt)."""

    system_prompt = (
        "You are the Maintainer in an automated code review pipeline. "
        "You review real, arbitrary user-submitted code and must be precise, "
        "not generic. Respond with ONLY a single JSON object — no markdown "
        "code fences, no prose before or after it. Schema:\n"
        "{\n"
        '  "summary": "2-4 sentence overview of the submission",\n'
        '  "findings": [{"dimension": "correctness|security|performance|maintainability", '
        '"severity": "low|medium|high", "line": <int or null>, "message": "...", '
        '"matched_rule_ref": "<historical rule id if this violates one, else null>"}],\n'
        '  "architecture_notes": ["..."],\n'
        '  "optimization_notes": ["..."]\n'
        "}\n"
        "Only report findings NOT already listed under 'Static analyzer findings' below "
        "— your value is catching what a deterministic tool can't: logic errors, "
        "architectural issues, and violations of the team's historical rules."
    )

    parts = [f"Language: {language_name}", "", "Submitted code:", "```", code, "```"]

    if ast_summary:
        classes = ", ".join(c["name"] for c in ast_summary.get("classes", [])) or "none"
        functions = ", ".join(f["name"] for f in ast_summary.get("functions", [])) or "none"
        parts += ["", f"AST structure — classes: {classes} | functions: {functions}"]

    if static_tool_available:
        if static_findings:
            parts.append("\nStatic analyzer findings (already detected, do not repeat):")
            for f in static_findings:
                parts.append(f"  - line {f.line} [{f.rule_code}] {f.message}")
        else:
            parts.append("\nStatic analyzer ran and found no issues.")
    else:
        parts.append(
            "\nNo deterministic static analyzer is available for this language yet — "
            "you are the only source of findings, so be thorough."
        )

    if grounded_rules:
        parts.append("\nHistorical review rules for this team:")
        for rule, score in grounded_rules:
            tag = f" (similarity={score:.2f})" if score is not None else ""
            parts.append(f"  - #{rule.rule_ref} [{rule.type}] {rule.description}{tag}")
        parts.append(
            "If the code violates any of these, set matched_rule_ref to that rule's id."
        )

    return system_prompt, "\n".join(parts)


async def review_submission(
    code: str,
    filename: str,
    language_hint: Optional[str],
    org_id: str,
    rules_index: RulesIndex,
    llm_caller=None,
) -> ReviewResult:
    """
    llm_caller: injected for testability — defaults to
    rate_limiter.run_llm_with_rate_limit, but tests pass a stub so the
    orchestration logic can be verified without a live Groq call.
    """
    if llm_caller is None:
        from rate_limiter import run_llm_with_rate_limit as llm_caller

    ext = detect_extension(filename, language_hint)
    language_name = EXTENSION_TO_LANGUAGE_NAME.get(ext, ext.lstrip(".") or "unknown")

    parser = TreeSitterParser()
    ast_summary = parser.parse_source(code, ext, filepath=filename)

    static_findings, static_tool_available = await run_static_analysis(code, ext)

    grounded_rules = select_rules_for_grounding(rules_index, code)

    system_prompt, user_prompt = build_review_prompt(
        code, language_name, ast_summary, static_findings, static_tool_available, grounded_rules
    )

    raw_response = await llm_caller(system_prompt, user_prompt, estimated_tokens=3000)
    llm_review = parse_llm_review_response(raw_response)

    tests_detected = detect_tests(code)
    all_findings = list(static_findings) + list(llm_review.findings)
    rubric = compute_rubric_score(all_findings, tests_detected)

    matched_rules_output = [
        {
            "rule_ref": rule.rule_ref,
            "type": rule.type,
            "description": rule.description,
            "similarity": score,
        }
        for rule, score in grounded_rules
        if score is None
        or any(f.matched_rule_ref == rule.rule_ref for f in llm_review.findings)
    ]

    return ReviewResult(
        language=language_name,
        rubric=rubric,
        summary=llm_review.summary,
        findings=all_findings,
        architecture_notes=llm_review.architecture_notes,
        optimization_notes=llm_review.optimization_notes,
        matched_historical_rules=matched_rules_output,
        static_analysis_available=static_tool_available,
        llm_parse_ok=llm_review.parse_ok,
    )
