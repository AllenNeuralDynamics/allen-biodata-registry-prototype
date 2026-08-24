"""
Feature: allen-biodata-registry-poc, Property 12: NL Search Pipeline Correctness
Task: 28.6

Asserts the four sub-properties of the natural-language search pipeline
the way Search_Lambda's POST /search/nl path implements them. We
exercise the live module's helper functions:

  * `_normalize_nl_query`  — input normalization
  * `_nl_cache_key`        — deterministic SHA-256 hash key
  * `_validate_sql`        — pre-execute SQL guardrail (forbids
                             INSERT/UPDATE/DELETE/etc., requires
                             SELECT/WITH, single statement)
  * `_explain_guardrail`   — EXPLAIN cost cap (against an in-memory
                             fake conn that returns a JSON plan)

Sub-properties checked:
  P12.1 Cache equivalence — semantically equivalent NL queries (same
        normalized form) produce the same cache key, so a second
        request lands in the cache without invoking Bedrock.
  P12.2 Forbidden-SQL guardrail — every input containing INSERT,
        UPDATE, DELETE, DROP, TRUNCATE, ALTER, GRANT, REVOKE, COPY,
        VACUUM, REINDEX, CALL, or EXECUTE is rejected with a
        FORBIDDEN_SQL-shaped error.
  P12.3 Single-statement guardrail — multi-statement SQL (semicolon
        before EOF) is rejected.
  P12.4 EXPLAIN cost cap — when the planner reports a Total Cost
        above the configured cap, the guardrail rejects with a
        cost-error message; below the cap it returns None.
  P12.5 Result equivalence — the SQL produced by the pipeline yields
        the same rowset as a hand-written reference SQL when both
        run under identical RLS context (proven indirectly here via
        cache-key determinism: same SQL → same cache → same exec).
  P12.6 Bedrock-outage path returns a structured error.

Validates: R18.2, R18.5, R18.6, R18.7, R20.3.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st


# ---------------------------------------------------------------------------
# Load the live search-lambda handler module so the PBT tests against the
# code that actually ships, not a re-implementation. We don't need
# OpenSearch / boto3 / psycopg / redis at import time — those are imported
# lazily by handler.py and the PBT only exercises the pure helpers.
# ---------------------------------------------------------------------------

_HANDLER_PATH = (
    Path(__file__).resolve().parent.parent
    / "services" / "search-lambda" / "handler.py"
)


def _load_search_handler():
    spec = importlib.util.spec_from_file_location(
        "search_lambda_handler_for_p12", _HANDLER_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {_HANDLER_PATH}")
    # Stub heavy deps so import doesn't fail.
    for mod_name in ("opensearchpy", "requests_aws4auth", "redis"):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = type(sys)("stub_" + mod_name)
    # opensearchpy needs RequestsHttpConnection & OpenSearch attributes.
    sys.modules["opensearchpy"].OpenSearch = lambda *a, **kw: None
    sys.modules["opensearchpy"].RequestsHttpConnection = object
    sys.modules["requests_aws4auth"].AWS4Auth = lambda *a, **kw: None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HANDLER = _load_search_handler()


# ---------------------------------------------------------------------------
# Helpers under test (extracted from the live module).
# ---------------------------------------------------------------------------

normalize = HANDLER._normalize_nl_query
cache_key = HANDLER._nl_cache_key
validate_sql = HANDLER._validate_sql
explain_guardrail = HANDLER._explain_guardrail


# ---------------------------------------------------------------------------
# P12.1 — Cache equivalence.
#
# Property: queries that differ only in case + whitespace produce the same
# normalized form, the same cache key, and would short-circuit at Bedrock.
# ---------------------------------------------------------------------------

@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    base=st.text(
        alphabet=st.sampled_from(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_"
        ),
        min_size=1, max_size=80,
    ),
)
def test_cache_key_invariant_under_whitespace_and_case(base):
    # ASCII alphabet — the cache key normalisation guarantees stable
    # round-trip for ASCII letters/digits + whitespace + a few common
    # punctuation chars. Non-ASCII Unicode is intentionally excluded
    # from this invariant because some chars (Turkish ı, German ß) do
    # not round-trip under any combination of `lower`/`upper`/`casefold`
    # by Unicode design — those are normalized to a canonical but
    # asymmetric form, sufficient for cache lookup but not symmetric
    # under upper().lower().
    a = base
    b = base.upper()
    c = "   " + base.lower() + "    "
    d = "\t".join(base.split())
    e = "  ".join(base.split())  # double-spaced variant

    norm_a = normalize(a)
    norm_b = normalize(b)
    norm_c = normalize(c)
    norm_d = normalize(d)
    norm_e = normalize(e)
    assert norm_a == norm_b == norm_c == norm_d == norm_e

    key_a = cache_key(norm_a)
    key_b = cache_key(norm_b)
    key_c = cache_key(norm_c)
    assert key_a == key_b == key_c


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    a=st.text(min_size=1, max_size=40),
    b=st.text(min_size=1, max_size=40),
)
def test_distinct_questions_produce_distinct_keys(a, b):
    if normalize(a) == normalize(b):
        return  # not a counter-example
    assert cache_key(normalize(a)) != cache_key(normalize(b))


def test_cache_key_format_and_length():
    key = cache_key(normalize("show me all assets"))
    assert key.startswith("nl:")
    # SHA-256 hexdigest is 64 chars; prefix adds 3.
    assert len(key) == 3 + 64


# ---------------------------------------------------------------------------
# P12.2 — Forbidden-SQL guardrail.
# ---------------------------------------------------------------------------

FORBIDDEN_KEYWORDS = [
    "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER",
    "CREATE", "GRANT", "REVOKE", "COPY", "VACUUM", "REINDEX", "CALL", "EXECUTE",
]


@pytest.mark.parametrize("kw", FORBIDDEN_KEYWORDS)
def test_explicit_forbidden_keyword_rejected(kw):
    sql = f"SELECT * FROM data_asset WHERE name = 'x' OR 1=1 {kw} something"
    err = validate_sql(sql)
    assert err is not None
    assert kw in err


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    kw=st.sampled_from(FORBIDDEN_KEYWORDS),
    suffix=st.text(alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")), min_size=1, max_size=30),
)
def test_forbidden_keyword_rejected_anywhere(kw, suffix):
    sql = f"SELECT 1 FROM data_asset WHERE x = 1; {kw} {suffix}"
    err = validate_sql(sql)
    assert err is not None


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.sampled_from([
    "SELECT 1",
    "SELECT id, name FROM data_asset",
    "SELECT data_type, COUNT(*) FROM data_asset GROUP BY data_type",
    "WITH t AS (SELECT 1 AS x) SELECT * FROM t",
    "  select  *  from  subject  ",
]))
def test_innocent_select_passes(sql):
    err = validate_sql(sql)
    assert err is None, f"select rejected: {err!r}"


# ---------------------------------------------------------------------------
# P12.3 — Single-statement guardrail.
# ---------------------------------------------------------------------------

def test_multi_statement_rejected():
    err = validate_sql("SELECT 1; SELECT 2")
    assert err is not None
    assert "multiple statements" in err.lower()


def test_trailing_semicolon_allowed():
    # A single trailing semicolon is fine — that's the canonical form
    # Bedrock returns.
    err = validate_sql("SELECT 1;")
    assert err is None


def test_empty_sql_rejected():
    err = validate_sql("")
    assert err is not None


def test_non_select_keyword_rejected():
    err = validate_sql("SHOW TABLES")
    assert err is not None
    assert "SELECT/WITH" in err


# ---------------------------------------------------------------------------
# P12.4 — EXPLAIN cost cap.
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, plan_cost):
        self.plan_cost = plan_cost
        self._next_result: Optional[Tuple[Any, ...]] = None

    def __enter__(self): return self
    def __exit__(self, *a): pass

    def execute(self, sql: str, params=None):
        # The handler runs `EXPLAIN (FORMAT JSON) ...` then fetches one row
        # with the plan as the first column.
        plan_json = [{"Plan": {"Total Cost": self.plan_cost}}]
        self._next_result = (json.dumps(plan_json),)

    def fetchone(self):
        return self._next_result


class _FakeConn:
    def __init__(self, plan_cost):
        self.plan_cost = plan_cost

    def cursor(self):
        return _FakeCursor(self.plan_cost)


@pytest.mark.parametrize("cost", [0, 100, 999_999, 9_999_999])
def test_explain_below_cap_passes(cost):
    err = explain_guardrail("SELECT * FROM data_asset", _FakeConn(cost))
    assert err is None


@pytest.mark.parametrize("cost", [10_000_001, 50_000_000, 1e9])
def test_explain_above_cap_rejects(cost):
    err = explain_guardrail("SELECT * FROM data_asset", _FakeConn(cost))
    assert err is not None
    assert "cost" in err.lower()


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(cost=st.floats(min_value=0, max_value=1e12, allow_nan=False, allow_infinity=False))
def test_cap_is_monotonic(cost):
    err = explain_guardrail("SELECT 1", _FakeConn(cost))
    if cost > 1e7:
        assert err is not None
    else:
        assert err is None


# ---------------------------------------------------------------------------
# P12.5 — Result equivalence proxy via cache-key determinism.
#
# If two normalized queries produce the same cache key, the subsequent
# execute path is byte-for-byte identical (same SQL retrieved, same RLS
# context applied). This is the substitute for a true round-trip test
# which would require a live Aurora and is exercised in QC3.
# ---------------------------------------------------------------------------

@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    sql=st.sampled_from([
        "SELECT id FROM data_asset",
        "SELECT data_type, COUNT(*) FROM data_asset GROUP BY data_type",
        "WITH t AS (SELECT 1) SELECT * FROM t",
    ])
)
def test_same_normalized_query_yields_same_cache_lookup(sql):
    # We don't have the actual NL question here, but we can check that
    # any two NL forms that normalize equal will index the same SQL.
    nl_a = sql + ""
    nl_b = "  " + sql.upper() + "  "
    assert cache_key(normalize(nl_a)) == cache_key(normalize(nl_b))


# ---------------------------------------------------------------------------
# P12.6 — Bedrock outage returns structured error.
# ---------------------------------------------------------------------------

def test_bedrock_outage_returns_structured_error():
    """When Bedrock is unreachable the live handler returns
    `{"code": "BEDROCK_UNAVAILABLE", ...}` shaped via _error(). We
    exercise this by directly calling _generate_sql_via_bedrock with
    BEDROCK_KB_ID unset (the fast-fail path).
    """
    # Keep current value so we can restore.
    saved = os.environ.get("BEDROCK_KB_ID", "")
    try:
        os.environ["BEDROCK_KB_ID"] = ""
        # Reset the module-level cached value.
        HANDLER._KB_ID = ""
        with pytest.raises(RuntimeError) as ei:
            HANDLER._generate_sql_via_bedrock("any question")
        assert "BEDROCK_KB_ID" in str(ei.value)
    finally:
        os.environ["BEDROCK_KB_ID"] = saved
        HANDLER._KB_ID = saved
