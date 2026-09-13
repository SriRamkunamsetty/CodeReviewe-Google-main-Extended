"""
Coverage for improvement #4.3, the 1-10 quality rating engine.
Includes a regression test for a real calibration bug found during
development: a naive weighted average let a single SQL-injection finding
get diluted by otherwise-clean dimensions, scoring 8.6/10.
"""

from scoring import compute_rubric_score, detect_tests
from static_analysis import StaticFinding


def test_clean_code_scores_ten():
    result = compute_rubric_score([], tests_detected=False)
    assert result.overall_score == 10.0
    assert "tests" not in result.dimension_scores


def test_tests_dimension_included_when_tests_detected():
    result = compute_rubric_score([], tests_detected=True)
    assert "tests" in result.dimension_scores
    assert result.weights_used["tests"] == 0.1


def test_high_severity_security_finding_is_capped_not_averaged_away():
    findings = [
        StaticFinding(dimension="security", severity="high", line=1, message="SQL injection", rule_code="S608"),
        StaticFinding(dimension="maintainability", severity="low", line=3, message="line too long", rule_code="E501"),
        StaticFinding(dimension="maintainability", severity="low", line=5, message="ambiguous name", rule_code="E741"),
    ]
    result = compute_rubric_score(findings, tests_detected=False)
    # Before the cap was added, this scored 8.6 — indefensible for code
    # containing a live SQL injection vulnerability.
    assert result.overall_score <= 4.0
    assert any("capped" in note.lower() for note in result.notes)


def test_medium_severity_security_finding_is_not_hard_capped():
    findings = [
        StaticFinding(dimension="security", severity="medium", line=2, message="weak hash", rule_code="S303"),
    ]
    result = compute_rubric_score(findings, tests_detected=False)
    assert result.overall_score > 4.0  # dinged, but not hard-capped like a HIGH finding


def test_score_never_drops_below_floor_of_one():
    findings = [
        StaticFinding(dimension="correctness", severity="high", line=i, message="bug", rule_code="F821")
        for i in range(20)
    ]
    result = compute_rubric_score(findings, tests_detected=False)
    assert result.overall_score >= 1.0


def test_detect_tests_recognizes_pytest_and_language_specific_patterns():
    assert detect_tests("import pytest\ndef test_add():\n    assert add(1,2)==3\n")
    assert detect_tests("#[test]\nfn it_works() { assert_eq!(2+2, 4); }")
    assert detect_tests("func TestAdd(t *testing.T) { }")
    assert not detect_tests("def add(a, b):\n    return a + b\n")
