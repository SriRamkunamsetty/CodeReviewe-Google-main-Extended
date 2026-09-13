"""
Coverage for improvement #4.4, "Historical Rule Ingestion & RAG Grounding".
Uses the exact CSV schema given in Problem Statement 1's evaluation spec.
"""

from rules_engine import parse_rules_csv, RulesIndex, select_rules_for_grounding

SAMPLE_CSV = """id,type,description
1,formatting,Avoid single-character variable names — they hurt readability
2,performance,Cache repeated database lookups inside the request loop
3,security,Never interpolate raw user input directly into SQL queries
"""


def test_parse_rules_csv_matches_problem_statement_schema():
    rules = parse_rules_csv(SAMPLE_CSV)
    assert len(rules) == 3
    assert rules[0].rule_ref == "1"
    assert rules[0].type == "formatting"
    assert "single-character" in rules[0].description
    assert rules[2].type == "security"


def test_parse_rules_csv_handles_missing_header():
    headerless = "1,formatting,Avoid single-character variable names\n2,security,Never trust user input\n"
    rules = parse_rules_csv(headerless)
    assert len(rules) == 2
    assert rules[1].type == "security"


def test_parse_rules_csv_empty_input():
    assert parse_rules_csv("") == []
    assert parse_rules_csv("   \n  \n") == []


def test_retrieval_returns_well_formed_scored_matches():
    """
    Raw top-k TF-IDF retrieval is lexical, not semantic — it can rank an
    unexpected rule highest when vocabulary overlaps in surprising ways
    (e.g. the word "user" pulling in the security rule instead of the
    single-char-variable rule for code that merely loops over `users`).
    This is exactly why select_rules_for_grounding() falls back to full
    inclusion for small rule sets rather than trusting retrieval alone
    (see the regression test below). This test only asserts the
    structural contract: retrieve() returns valid (rule, score) pairs
    with scores in range, not which specific rule "should" win.
    """
    index = RulesIndex()
    index.fit(parse_rules_csv(SAMPLE_CSV))

    code = "for user in users:\n    a = user.id\n    b = user.name\n"
    matches = index.retrieve(code, top_k=3)
    for rule, score in matches:
        assert rule.rule_ref in {"1", "2", "3"}
        assert 0.0 <= score <= 1.0


def test_full_inclusion_strategy_grounds_low_vocabulary_overlap_case():
    """
    Regression test for the real gap found during development: pure top-k
    TF-IDF retrieval missed a textbook SQL-injection snippet because it
    shares almost no vocabulary with the rule text. The hybrid strategy
    must include ALL rules when the rule set is small enough to fit in a
    prompt (<=25 rules), guaranteeing this case is always grounded.
    """
    index = RulesIndex()
    index.fit(parse_rules_csv(SAMPLE_CSV))

    sql_injection_code = (
        'query = "SELECT * FROM users WHERE id = " + user_input\n'
        "cursor.execute(query)\n"
    )
    selected = select_rules_for_grounding(index, sql_injection_code)
    selected_refs = {rule.rule_ref for rule, _ in selected}
    assert selected_refs == {"1", "2", "3"}  # full inclusion, all 3 rules present
    assert "3" in selected_refs  # the security rule specifically must be there


def test_select_rules_for_grounding_falls_back_to_retrieval_above_threshold():
    many_rules_csv = "id,type,description\n" + "\n".join(
        f"{i},style,Rule number {i} about something unrelated to database access"
        for i in range(1, 40)
    )
    index = RulesIndex()
    index.fit(parse_rules_csv(many_rules_csv))
    assert index.count() == 39

    selected = select_rules_for_grounding(index, "some code", top_k=5)
    assert len(selected) <= 5  # retrieval path caps results, unlike full-inclusion


def test_empty_index_returns_no_selection():
    index = RulesIndex()
    assert index.is_empty()
    assert select_rules_for_grounding(index, "any code") == []
