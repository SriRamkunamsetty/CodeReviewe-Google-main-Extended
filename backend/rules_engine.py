"""
Historical Rule Ingestion & Retrieval Engine
=============================================

Implements improvement #4.4 from the AutoMaintainer Extension Report:
"Historical Rule Ingestion & RAG Grounding".

Given a CSV of historical review rules in the schema:

    id, type, description
    1, formatting, Avoid single-character variable names — they hurt readability
    2, performance, Cache repeated database lookups inside the request loop
    3, security, Never interpolate raw user input directly into SQL queries

...this module embeds each rule and lets the review engine retrieve the
top-k rules most semantically relevant to a given code submission, so the
Maintainer's review can cite *why* a finding matters ("violates historical
rule #3") instead of asserting it from nowhere.

Design choice — TF-IDF instead of a neural embedding model:
    The evaluation rule sets described in the problem statement are small
    (a handful to a few dozen rows), not a web-scale corpus. A neural
    embedding model (sentence-transformers, OpenAI embeddings, etc.) would
    add a heavy dependency (torch) or an external API call for a retrieval
    problem that scikit-learn's TF-IDF + cosine similarity solves in
    milliseconds with zero network dependency and zero extra cost per
    review. If the rule set later grows into the thousands and needs true
    semantic matching (synonyms, paraphrases), swap `RulesIndex` for a
    pgvector-backed store — the public interface (`fit`/`retrieve`) is
    designed so that swap doesn't touch any calling code.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from typing import Optional

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

logger = logging.getLogger(__name__)


@dataclass
class HistoricalRule:
    rule_ref: str  # the id/reference from the source CSV (kept as string; may be non-numeric)
    type: str
    description: str

    def as_text(self) -> str:
        """Text used for embedding — combines type and description so a
        query like 'SQL string built with +' still matches a rule typed
        'security' even if the word 'security' never appears in the code."""
        return f"{self.type}: {self.description}"


def parse_rules_csv(csv_text: str) -> list[HistoricalRule]:
    """
    Parse a rules CSV in the <id>, <type>, <description> schema.
    Tolerant of a missing header (falls back to positional columns) and of
    extra whitespace around fields, since submitted CSVs during evaluation
    may not be perfectly clean.
    """
    if not csv_text or not csv_text.strip():
        return []

    reader = csv.reader(io.StringIO(csv_text.strip()))
    rows = [row for row in reader if row and any(cell.strip() for cell in row)]
    if not rows:
        return []

    header = [c.strip().lower() for c in rows[0]]
    data_rows = rows
    id_idx, type_idx, desc_idx = 0, 1, 2

    if {"id", "type", "description"}.issubset(set(header)):
        id_idx = header.index("id")
        type_idx = header.index("type")
        desc_idx = header.index("description")
        data_rows = rows[1:]

    rules: list[HistoricalRule] = []
    for row in data_rows:
        if len(row) <= max(id_idx, type_idx, desc_idx):
            continue
        rule_ref = row[id_idx].strip()
        rule_type = row[type_idx].strip()
        description = row[desc_idx].strip()
        if not description:
            continue
        rules.append(
            HistoricalRule(rule_ref=rule_ref, type=rule_type, description=description)
        )
    return rules


class RulesIndex:
    """
    In-memory TF-IDF index over a set of historical rules for one
    organization. Cheap enough to rebuild on every ingestion — typical
    rule sets are tens to low hundreds of rows, not millions.
    """

    def __init__(self):
        self._rules: list[HistoricalRule] = []
        self._vectorizer: Optional[TfidfVectorizer] = None
        self._matrix = None

    def fit(self, rules: list[HistoricalRule]) -> None:
        self._rules = rules
        if not rules:
            self._vectorizer = None
            self._matrix = None
            return
        self._vectorizer = TfidfVectorizer(
            lowercase=True,
            stop_words="english",
            ngram_range=(1, 2),
        )
        self._matrix = self._vectorizer.fit_transform([r.as_text() for r in rules])

    def is_empty(self) -> bool:
        return not self._rules

    def count(self) -> int:
        return len(self._rules)

    def all_rules(self) -> list[HistoricalRule]:
        return list(self._rules)

    def retrieve(
        self, query_text: str, top_k: int = 3, min_score: float = 0.05
    ) -> list[tuple[HistoricalRule, float]]:
        """
        Return up to top_k (rule, similarity_score) pairs most relevant to
        query_text, filtered to a minimum similarity so unrelated code
        doesn't get spuriously matched to an unrelated rule.
        """
        if self.is_empty() or not query_text.strip():
            return []

        query_vec = self._vectorizer.transform([query_text])
        scores = cosine_similarity(query_vec, self._matrix)[0]

        ranked = sorted(
            zip(self._rules, scores), key=lambda pair: pair[1], reverse=True
        )
        return [(rule, float(score)) for rule, score in ranked[:top_k] if score >= min_score]


def select_rules_for_grounding(
    index: "RulesIndex",
    query_text: str,
    top_k: int = 5,
    full_inclusion_threshold: int = 25,
) -> list[tuple[HistoricalRule, Optional[float]]]:
    """
    Hybrid rule-selection strategy, chosen after empirically testing pure
    top-k TF-IDF retrieval against the sample rules CSV from the problem
    statement: a 3-word rule ("Never interpolate raw user input into SQL")
    can share almost no vocabulary with a real violation
    (``query = "SELECT ... " + user_input``), so pure lexical retrieval
    silently drops a rule that a reviewer clearly should have cited.

    The fix: when an org's rule set is small enough to fit comfortably in
    an LLM prompt (<= full_inclusion_threshold rules — true for every
    example in the problem statement, and realistically true for most
    teams' style guides), include *all* of them every time. Retrieval
    only kicks in once a rule set grows past the point where "just include
    everything" would blow the context budget, at which point some rules
    being missed is an acceptable, documented tradeoff rather than a
    silent one.

    Returns (rule, score) pairs; score is None for rules included via the
    "small rule set" path rather than by similarity ranking.
    """
    if index.is_empty():
        return []

    if index.count() <= full_inclusion_threshold:
        return [(rule, None) for rule in index.all_rules()]

    return index.retrieve(query_text, top_k=top_k)


# ---------------------------------------------------------------------------
# Supabase persistence + per-org index cache
# ---------------------------------------------------------------------------
# Rules are stored durably in the `historical_rules` table (see
# supabase_schema_review_extension.sql) so they survive worker restarts.
# The fitted TF-IDF index is cached in-process per org and rebuilt whenever
# new rules are ingested for that org — this avoids re-fitting on every
# single review request.

_INDEX_CACHE: dict[str, RulesIndex] = {}


def invalidate_index_cache(org_id: str) -> None:
    _INDEX_CACHE.pop(org_id, None)


async def ingest_rules_for_org(supabase_client, org_id: str, csv_text: str) -> int:
    """
    Parse a CSV and upsert its rules into `historical_rules` for the given
    org, then invalidate the cached index so the next review picks up the
    new rules. Returns the number of rules ingested.
    """
    import asyncio

    rules = parse_rules_csv(csv_text)
    if not rules:
        return 0

    rows = [
        {
            "org_id": org_id,
            "rule_ref": r.rule_ref,
            "type": r.type,
            "description": r.description,
        }
        for r in rules
    ]

    await asyncio.to_thread(
        lambda: supabase_client.table("historical_rules")
        .upsert(rows, on_conflict="org_id,rule_ref")
        .execute()
    )

    invalidate_index_cache(org_id)
    return len(rules)


async def get_or_build_index(supabase_client, org_id: str) -> RulesIndex:
    """Fetch (with in-process caching) the fitted RulesIndex for an org."""
    import asyncio

    cached = _INDEX_CACHE.get(org_id)
    if cached is not None:
        return cached

    index = RulesIndex()
    if supabase_client is None:
        return index  # empty index — review proceeds without grounding

    try:
        result = await asyncio.to_thread(
            lambda: supabase_client.table("historical_rules")
            .select("rule_ref, type, description")
            .eq("org_id", org_id)
            .execute()
        )
        rows = result.data or []
        rules = [
            HistoricalRule(
                rule_ref=str(row.get("rule_ref", "")),
                type=row.get("type", ""),
                description=row.get("description", ""),
            )
            for row in rows
        ]
        index.fit(rules)
    except Exception as e:
        logger.warning(f"Failed to load historical rules for org {org_id}: {e}")

    _INDEX_CACHE[org_id] = index
    return index
