"""
Coverage for review_engine.py (improvement #4.1).

Two things are tested separately on purpose:
1. parse_llm_review_response — pure function, tested against hand-written
   sample model outputs (clean JSON, fenced JSON, JSON with surrounding
   prose, and garbage) since a live Groq call isn't reachable from this
   sandbox.
2. review_submission — the full orchestration path, tested with an
   injected `llm_caller` stub so the AST/static-analysis/rules/scoring
   wiring is verified without needing real network access or API keys.
"""

import pytest

from review_engine import parse_llm_review_response, review_submission
from rules_engine import RulesIndex, parse_rules_csv

SAMPLE_RULES_CSV = """id,type,description
1,formatting,Avoid single-character variable names — they hurt readability
2,performance,Cache repeated database lookups inside the request loop
3,security,Never interpolate raw user input directly into SQL queries
"""

CLEAN_JSON_RESPONSE = """{
  "summary": "Simple addition function with no issues.",
  "findings": [],
  "architecture_notes": ["Function is appropriately small and single-purpose."],
  "optimization_notes": []
}"""

FENCED_JSON_RESPONSE = """```json
{
  "summary": "Uses string concatenation to build a SQL query.",
  "findings": [
    {"dimension": "security", "severity": "high", "line": 1, "message": "Raw string concatenation into a SQL query allows injection.", "matched_rule_ref": "3"}
  ],
  "architecture_notes": [],
  "optimization_notes": ["Consider parameterized queries via the DB driver's placeholder syntax."]
}
```"""

PROSE_WRAPPED_JSON_RESPONSE = """Sure, here's my review:

{
  "summary": "Loop performs a query per iteration.",
  "findings": [
    {"dimension": "performance", "severity": "medium", "line": 2, "message": "N+1 query pattern inside loop.", "matched_rule_ref": "2"}
  ],
  "architecture_notes": [],
  "optimization_notes": ["Batch the lookups outside the loop."]
}

Let me know if you'd like more detail."""

GARBAGE_RESPONSE = "I reviewed the code and it looks mostly fine, no major issues to report here."


def test_parse_clean_json():
    result = parse_llm_review_response(CLEAN_JSON_RESPONSE)
    assert result.parse_ok
    assert result.findings == []
    assert "addition" in result.summary.lower()


def test_parse_fenced_json():
    result = parse_llm_review_response(FENCED_JSON_RESPONSE)
    assert result.parse_ok
    assert len(result.findings) == 1
    assert result.findings[0].dimension == "security"
    assert result.findings[0].severity == "high"
    assert result.findings[0].matched_rule_ref == "3"


def test_parse_json_with_surrounding_prose():
    result = parse_llm_review_response(PROSE_WRAPPED_JSON_RESPONSE)
    assert result.parse_ok
    assert len(result.findings) == 1
    assert result.findings[0].matched_rule_ref == "2"


def test_parse_garbage_degrades_gracefully_instead_of_crashing():
    result = parse_llm_review_response(GARBAGE_RESPONSE)
    assert result.parse_ok is False
    assert result.findings == []
    assert "mostly fine" in result.summary  # raw text preserved, not silently dropped


def test_parse_drops_malformed_individual_finding_without_failing_whole_response():
    response_with_bad_finding = """{
      "summary": "test",
      "findings": [
        {"dimension": "not-a-real-dimension", "severity": "high", "line": 1, "message": "bad"},
        {"dimension": "security", "severity": "high", "line": 2, "message": "good one", "matched_rule_ref": "3"}
      ],
      "architecture_notes": [],
      "optimization_notes": []
    }"""
    result = parse_llm_review_response(response_with_bad_finding)
    assert result.parse_ok
    assert len(result.findings) == 1  # bad-dimension finding silently skipped
    assert result.findings[0].message == "good one"


@pytest.mark.asyncio
async def test_review_submission_end_to_end_with_stubbed_llm():
    rules_index = RulesIndex()
    rules_index.fit(parse_rules_csv(SAMPLE_RULES_CSV))

    async def stub_llm_caller(system_prompt, user_prompt, **kwargs):
        return FENCED_JSON_RESPONSE

    sql_injection_code = (
        'def get_user(user_input):\n'
        '    query = "SELECT * FROM users WHERE id = " + user_input\n'
        "    return db.execute(query)\n"
    )

    result = await review_submission(
        code=sql_injection_code,
        filename="submission.py",
        language_hint=None,
        org_id="test-org",
        rules_index=rules_index,
        llm_caller=stub_llm_caller,
    )

    assert result.language == "Python"
    assert result.static_analysis_available is True  # ruff supports .py
    assert result.llm_parse_ok is True
    # ruff should independently flag something here too (e.g. unused-ish patterns
    # or at minimum the merge shouldn't crash); the LLM's high-severity security
    # finding must be present regardless.
    assert any(f.dimension == "security" and f.severity == "high" for f in result.findings)
    # A high-severity security finding must cap the score low (see test_scoring.py)
    assert result.rubric.overall_score <= 4.0


@pytest.mark.asyncio
async def test_review_submission_handles_unsupported_language_gracefully():
    rules_index = RulesIndex()
    rules_index.fit(parse_rules_csv(SAMPLE_RULES_CSV))

    async def stub_llm_caller(system_prompt, user_prompt, **kwargs):
        return CLEAN_JSON_RESPONSE

    result = await review_submission(
        code="(display (+ 1 2))",
        filename="submission.scm",  # Scheme — genuinely unsupported
        language_hint=None,
        org_id="test-org",
        rules_index=rules_index,
        llm_caller=stub_llm_caller,
    )

    assert result.static_analysis_available is False
    assert result.llm_parse_ok is True
    assert result.rubric.overall_score == 10.0  # no findings reported -> clean
