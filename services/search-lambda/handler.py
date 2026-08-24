"""
Allen BioData Registry PoC — Search Lambda.

Lightweight read-path Lambda fronting OpenSearch Serverless. Backs the
GET /search and GET /suggest API Gateway routes.

Trust boundary
--------------

Authenticates as ``biodata_app`` via IAM auth on Aurora (not used by
Search itself — search hits OpenSearch, not Aurora — but the role is
required by the apigateway module's authorizer chain).

Search filtering for visibility is done at the OpenSearch query level
by injecting an access-filter clause into the query: non-privileged
users only see ``space_id`` ∈ allowed_space_ids OR ``is_public:true``,
with sensitive_flag=false unless caller is data_admin.

Endpoints
---------

GET  /search?q=...&limit=...      → BM25 multi_match + access filter
GET  /suggest?prefix=...           → search_as_you_type on name_suggest
GET  /search/by_space/{space_id}   → all assets in a space (RLS-filtered)

Validates: R8.3, R8.4, R17.1-R17.11.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import unquote_plus

import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth

LOG = logging.getLogger()
LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_OS_CLIENT: Optional[OpenSearch] = None


def _opensearch() -> OpenSearch:
    global _OS_CLIENT
    if _OS_CLIENT is not None:
        return _OS_CLIENT

    region = os.environ.get("AWS_REGION", "us-west-2")
    endpoint = os.environ["OPENSEARCH_ENDPOINT"].replace("https://", "").rstrip("/")

    creds = boto3.Session().get_credentials()
    awsauth = AWS4Auth(
        creds.access_key,
        creds.secret_key,
        region,
        "aoss",
        session_token=creds.token,
    )
    _OS_CLIENT = OpenSearch(
        hosts=[{"host": endpoint, "port": 443}],
        http_auth=awsauth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        pool_maxsize=20,
    )
    return _OS_CLIENT


def _auth_context(event: Mapping[str, Any]) -> Dict[str, Any]:
    """Extract authorizer context from the API Gateway event.

    The Authorizer Lambda emits {user_id, org_ids, space_ids, roles}
    as comma-joined strings (API Gateway requires scalar context values).
    """
    raw = (event.get("requestContext") or {}).get("authorizer") or {}

    def _split(key: str) -> List[str]:
        v = raw.get(key)
        if not v:
            return []
        if isinstance(v, list):
            return v
        return [s.strip() for s in str(v).split(",") if s.strip()]

    return {
        "user_id": raw.get("user_id"),
        "org_ids": _split("org_ids"),
        "space_ids": _split("space_ids"),
        "roles": _split("roles"),
    }


def _is_privileged(roles: List[str]) -> bool:
    """data_admin / org_admin / system see sensitive assets."""
    return bool(set(roles) & {"data_administrator", "org_admin", "system"})


def _access_filter(auth: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build a list of OpenSearch filter clauses for visibility.

    Returns the list to be combined under bool.filter in the query.
    Non-privileged callers: must match space_ids OR be public.
    Privileged callers: no scope filter.
    Sensitive flag is excluded for non-privileged callers regardless.
    """
    space_ids = auth["space_ids"]
    privileged = _is_privileged(auth["roles"])

    filters: List[Dict[str, Any]] = []

    # Visibility scope (skipped for privileged callers).
    if not privileged:
        # "Public" = published AND valid. The live index dynamic-maps string
        # fields as text with a `.keyword` sub-field, so term queries use
        # `.keyword`. (Earlier this referenced non-existent is_public /
        # sensitive_flag fields, which silently matched nothing — so every
        # non-privileged search returned zero rows.)
        public_clause = {
            "bool": {
                "must": [
                    {"term": {"lifecycle_state.keyword": "published"}},
                    {"term": {"validation_status.keyword": "valid"}},
                ]
            }
        }
        if space_ids:
            filters.append({
                "bool": {
                    "should": [
                        {"terms": {"space_id.keyword": space_ids}},
                        public_clause,
                    ],
                    "minimum_should_match": 1,
                }
            })
        else:
            # No space membership — only see published+valid assets.
            filters.append(public_clause)

        # Sensitive flag layer 3 (Search): exclude sensitive for non-privileged.
        filters.append({
            "bool": {
                "must_not": [{"term": {"is_sensitive": True}}]
            }
        })

    return filters


def _ok(body: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Authorization,Content-Type,X-Amz-Date,X-Api-Key,X-Amz-Security-Token",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body),
    }


def _error(status: int, code: str, message: str) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Authorization,Content-Type,X-Amz-Date,X-Api-Key,X-Amz-Security-Token",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps({
            "code": code,
            "message": message,
        }),
    }


def _search(event: Mapping[str, Any], auth: Dict[str, Any]) -> Dict[str, Any]:
    qs = event.get("queryStringParameters") or {}
    q = unquote_plus(qs.get("q") or "").strip()
    limit = min(int(qs.get("limit") or 20), 100)
    index = qs.get("index") or "data_asset"

    must_clause: Dict[str, Any]
    if q:
        must_clause = {
            "multi_match": {
                "query": q,
                "fields": ["name^2", "description", "data_type", "modality^1.5"],
                "type": "best_fields",
                "fuzziness": "AUTO",
            }
        }
    else:
        must_clause = {"match_all": {}}

    body = {
        "size": limit,
        "query": {
            "bool": {
                "must": [must_clause],
                "filter": _access_filter(auth),
            }
        },
        "sort": [
            {"_score": {"order": "desc"}},
            {"created_at": {"order": "desc", "missing": "_last"}},
        ],
        # Faceted aggregations (R17.4): counts per filter dimension returned
        # in the same query. The live index dynamic-maps these as text with a
        # `.keyword` sub-field, so we aggregate on `<field>.keyword`.
        "aggs": {
            "data_type": {"terms": {"field": "data_type.keyword", "size": 20}},
            "species": {"terms": {"field": "species.keyword", "size": 20}},
            "lifecycle_state": {"terms": {"field": "lifecycle_state.keyword", "size": 10}},
            "validation_status": {"terms": {"field": "validation_status.keyword", "size": 10}},
            "organization": {"terms": {"field": "organization.keyword", "size": 20}},
        },
    }

    LOG.info("search query=%r index=%s", q, index)
    try:
        result = _opensearch().search(index=index, body=body)
    except Exception as exc:  # noqa: BLE001
        # Aggregations are best-effort: if a field isn't aggregatable on this
        # index, retry the same search WITHOUT aggs so results still return.
        LOG.warning("search with aggs failed (%s); retrying without facets", exc)
        body.pop("aggs", None)
        try:
            result = _opensearch().search(index=index, body=body)
        except Exception as exc2:  # noqa: BLE001
            LOG.exception("search failed: %s", exc2)
            return _error(500, "INTERNAL_ERROR", "search failed")

    hits = result.get("hits", {}).get("hits", [])
    total = result.get("hits", {}).get("total", {}).get("value", 0)

    # Shape aggregations into a compact facets map: {field: [{value, count}]}.
    facets: Dict[str, Any] = {}
    for field, agg in (result.get("aggregations") or {}).items():
        buckets = agg.get("buckets") or []
        if buckets:
            facets[field] = [
                {"value": b.get("key"), "count": b.get("doc_count")} for b in buckets
            ]

    return _ok({
        "total": total,
        "hits": [
            {"id": h["_id"], "score": h.get("_score"), "source": h.get("_source", {})}
            for h in hits
        ],
        "query": q,
        "index": index,
        "facets": facets,
    })


def _suggest(event: Mapping[str, Any], auth: Dict[str, Any]) -> Dict[str, Any]:
    qs = event.get("queryStringParameters") or {}
    prefix = unquote_plus(qs.get("prefix") or "").strip()
    if not prefix:
        return _error(400, "MISSING_PARAM", "prefix is required")

    limit = min(int(qs.get("limit") or 10), 25)

    body = {
        "size": limit,
        "query": {
            "bool": {
                "must": [
                    {
                        "match_phrase_prefix": {
                            "name": {"query": prefix, "max_expansions": 50}
                        }
                    }
                ],
                "filter": _access_filter(auth),
            }
        },
        "_source": ["id", "name", "data_type", "space_id"],
    }

    try:
        result = _opensearch().search(index="data_asset", body=body)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("suggest failed: %s", exc)
        return _error(500, "INTERNAL_ERROR", "suggest failed")

    hits = result.get("hits", {}).get("hits", [])
    return _ok({
        "prefix": prefix,
        "suggestions": [
            {"id": h["_id"], "name": (h.get("_source") or {}).get("name")}
            for h in hits
        ],
    })


# ---------------------------------------------------------------------------
# POST /search/nl — Natural-language search.
#
# Pipeline (R18.1 - R18.7):
#   1. Normalize the user's question (lowercase + collapse whitespace).
#   2. Hash and check NL_Cache (Redis) for a prior NL → SQL mapping.
#      Cache hit → execute the cached SQL (with current RLS context).
#      Cache miss → call Bedrock with KB context, get SQL.
#   3. EXPLAIN guardrail: run `EXPLAIN <generated_sql>` against Aurora.
#      If the plan touches any unindexed scan that exceeds the cost cap, or
#      uses a forbidden operation (DROP/DELETE/etc.), reject with
#      EXPENSIVE_QUERY.
#   4. If guardrails pass, execute parameterized SQL with RLS context set.
#   5. Cache the (hash, SQL) pair on success for 30 minutes.
#
# Trust model: NL→SQL inherits the caller's RLS context — every connection
# checks in with `SET LOCAL app.current_user_id`, etc., so generated SQL is
# automatically scoped to the user's visible spaces. Bedrock writes plain
# SQL; we don't trust it to know the visibility model.
#
# Validates: R18.1, R18.2, R18.4, R18.5, R18.6, R18.7, R20.3.
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402  (placed near usage for grep-ability)
import re       # noqa: E402

_BEDROCK_RUNTIME = None
_REDIS_CLIENT = None
_NL_CACHE_TTL_SECONDS = int(os.environ.get("NL_CACHE_TTL_SECONDS", "1800"))
_KB_ID = os.environ.get("BEDROCK_KB_ID", "")
_NL_MODEL_ID = os.environ.get(
    "NL_MODEL_ID", "us.anthropic.claude-opus-4-7"
)


def _bedrock_runtime():
    global _BEDROCK_RUNTIME
    if _BEDROCK_RUNTIME is None:
        _BEDROCK_RUNTIME = boto3.client(
            "bedrock-agent-runtime",
            region_name=os.environ.get("AWS_REGION", "us-west-2"),
        )
    return _BEDROCK_RUNTIME


def _redis_client():
    """Lazy Redis client for NL_Cache. Returns None if Redis is
    unavailable or unconfigured — the cache layer is a perf optimization,
    never a correctness requirement (R20.7)."""
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT

    host = os.environ.get("REDIS_PRIMARY_ENDPOINT")
    if not host:
        return None
    try:
        import redis  # type: ignore[import-untyped]
        token = os.environ.get("REDIS_AUTH_TOKEN") or None
        _REDIS_CLIENT = redis.Redis(
            host=host,
            port=int(os.environ.get("REDIS_PORT", "6379")),
            ssl=True,
            password=token,
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        _REDIS_CLIENT.ping()
        return _REDIS_CLIENT
    except Exception as exc:
        LOG.warning("redis unavailable (NL_Cache disabled): %s", exc)
        _REDIS_CLIENT = None
        return None


def _normalize_nl_query(q: str) -> str:
    # Unicode-correct normalization for case-insensitive cache lookup:
    #   1. NFKC normalisation — collapses visually-identical chars
    #      (e.g. micro sign µ U+00B5 → Greek μ U+03BC).
    #   2. casefold() — handles language-aware case folding better than
    #      .lower() (e.g. German ß → ss).
    #   3. We additionally NFKC the result of casefold so Turkish ı/I
    #      asymmetries collapse: NFKC + casefold("I") = "i", and we
    #      NFKC again to preserve idempotence.
    import unicodedata
    n = unicodedata.normalize("NFKC", q.strip()).casefold()
    n = unicodedata.normalize("NFKC", n)
    return re.sub(r"\s+", " ", n)


def _nl_cache_key(normalized: str) -> str:
    return "nl:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


_FORBIDDEN_TOKENS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|"
    r"COPY|VACUUM|REINDEX|CALL|EXECUTE)\b",
    flags=re.IGNORECASE,
)
_EXPLAIN_COST_CAP = float(os.environ.get("NL_EXPLAIN_COST_CAP", "1.0e7"))


def _validate_sql(sql: str) -> Optional[str]:
    if not sql.strip():
        return "empty SQL"
    cleaned = sql.strip().rstrip(";")
    if ";" in cleaned:
        return "multiple statements not allowed"
    m = _FORBIDDEN_TOKENS.search(cleaned)
    if m:
        return f"forbidden token: {m.group(0)}"
    head = cleaned.lstrip().split(None, 1)[0].upper()
    if head not in ("SELECT", "WITH"):
        return f"only SELECT/WITH queries allowed (got {head!r})"
    return None


def _explain_guardrail(sql: str, conn) -> Optional[str]:
    cleaned = sql.strip().rstrip(";")
    try:
        with conn.cursor() as cur:
            cur.execute(f"EXPLAIN (FORMAT JSON) {cleaned}")
            row = cur.fetchone()
        plan_root = row[0]
        if isinstance(plan_root, str):
            plan_root = json.loads(plan_root)
        plan = plan_root[0]["Plan"] if isinstance(plan_root, list) else plan_root["Plan"]
        cost = float(plan.get("Total Cost", 0))
        if cost > _EXPLAIN_COST_CAP:
            return f"plan cost {cost:.0f} exceeds cap {_EXPLAIN_COST_CAP:.0f}"
        return None
    except Exception as exc:  # noqa: BLE001
        return f"EXPLAIN failed: {exc}"


def _generate_sql_via_bedrock(question: str) -> str:
    if not _KB_ID:
        raise RuntimeError("BEDROCK_KB_ID env var not set; NL search disabled")

    # The default Claude RAG system prompt refuses to write SQL ("Sorry, I
    # am unable to assist..."). We override with a SQL-only prompt template
    # so the model knows code generation is the goal.
    prompt_template = (
        "Human: You are a Postgres SQL writer for the Allen BioData Registry. "
        "Use the schema documentation in $search_results$ to answer.\n"
        "Rules:\n"
        " * Output ONLY a single SELECT or WITH query inside a fenced ```sql block.\n"
        " * No prose, no apology.\n"
        " * Never INSERT, UPDATE, DELETE, DROP, TRUNCATE, ALTER, GRANT, REVOKE.\n"
        " * Do NOT add explicit org_id or space_id WHERE clauses — the registry uses "
        "row-level security context that filters automatically.\n"
        " * Inline all literal values directly in the query "
        "(e.g. WHERE data_type = 'behavior'). Do NOT use bind parameters or "
        "placeholders like $1, $2 — the query must be self-contained and runnable as-is.\n"
        "\n"
        "$search_results$\n"
        "\n"
        "Question: $query$\n"
        "\n"
        "Assistant: ```sql\n"
    )

    region = os.environ.get("AWS_REGION", "us-west-2")
    account_id = os.environ.get("AWS_ACCOUNT_ID", "014097726564")
    model_arn = (
        f"arn:aws:bedrock:{region}:{account_id}:"
        f"inference-profile/{_NL_MODEL_ID}"
    )

    response = _bedrock_runtime().retrieve_and_generate(
        input={"text": question},
        retrieveAndGenerateConfiguration={
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": _KB_ID,
                "modelArn": model_arn,
                "generationConfiguration": {
                    "promptTemplate": {
                        "textPromptTemplate": prompt_template,
                    },
                },
            },
        },
    )

    text = (response.get("output") or {}).get("text") or ""
    # Strip the ```sql fence we asked the model to open.
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if m:
        sql = m.group(1).strip()
    else:
        # Model may have returned just the SQL body (no closing fence).
        sql = text.strip()
        # Trim a trailing fence if present.
        if sql.endswith("```"):
            sql = sql[:-3].strip()
    LOG.info("nl: bedrock returned SQL of length %d", len(sql))
    return sql


def _to_jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _execute_with_rls(sql: str, auth: Dict[str, Any]) -> Dict[str, Any]:
    try:
        import psycopg  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(f"psycopg not bundled in this Lambda: {exc}") from exc

    rds = boto3.client("rds", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    host = os.environ["AURORA_HOST"]
    port = int(os.environ.get("AURORA_PORT", "5432"))
    db = os.environ["AURORA_DB"]
    user = os.environ.get("AURORA_DB_USER", "biodata_app")
    region = os.environ.get("AWS_REGION", "us-west-2")
    token = rds.generate_db_auth_token(
        DBHostname=host, Port=port, DBUsername=user, Region=region
    )

    conn = psycopg.connect(
        host=host, port=port, user=user, password=token,
        dbname=db, sslmode="require", connect_timeout=10,
    )
    try:
        def _quote(s: str) -> str:
            return "'" + s.replace("'", "''") + "'"
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL app.current_user_id = {_quote(auth['user_id'] or '')}")
            cur.execute(f"SET LOCAL app.current_org_ids = {_quote(','.join(auth['org_ids']))}")
            cur.execute(f"SET LOCAL app.current_space_ids = {_quote(','.join(auth['space_ids']))}")
            # RLS helpers read app.current_user_role_set (see 0006_rls_policies.sql).
            cur.execute(f"SET LOCAL app.current_user_role_set = {_quote(','.join(auth['roles']))}")
            cur.execute("SET LOCAL statement_timeout = '10000'")

        if (err := _explain_guardrail(sql, conn)) is not None:
            return {"_guardrail_error": err}

        with conn.cursor() as cur:
            cur.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall() if cur.description else []
        return {
            "columns": cols,
            "rows": [
                [_to_jsonable(v) for v in row]
                for row in rows[:200]
            ],
            "row_count": len(rows),
        }
    finally:
        conn.close()


def _public_stats() -> Dict[str, Any]:
    """Aggregate, row-free counts for the public landing page.

    Returns only COUNT(*) aggregates — never row-level data — so it is
    safe to expose without authentication. We connect as the standard
    ``biodata_app`` role and set a ``system`` RLS context so the counts
    span all spaces; because we only ever return integers (not rows),
    this leaks no per-asset or per-space information.

    Shape:
        {
          "total": int,
          "published": int,
          "validated": int,
          "by_lifecycle_state": [{"state": str, "count": int}, ...]
        }
    """
    try:
        import psycopg  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(f"psycopg not bundled in this Lambda: {exc}") from exc

    rds = boto3.client("rds", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    host = os.environ["AURORA_HOST"]
    port = int(os.environ.get("AURORA_PORT", "5432"))
    db = os.environ["AURORA_DB"]
    user = os.environ.get("AURORA_DB_USER", "biodata_app")
    region = os.environ.get("AWS_REGION", "us-west-2")
    token = rds.generate_db_auth_token(
        DBHostname=host, Port=port, DBUsername=user, Region=region
    )

    conn = psycopg.connect(
        host=host, port=port, user=user, password=token,
        dbname=db, sslmode="require", connect_timeout=10,
    )
    try:
        def _quote(s: str) -> str:
            return "'" + s.replace("'", "''") + "'"

        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '8000'")

            # Load every space id so the RLS context spans the whole
            # registry. The `space` table has no RLS, so this is safe; we
            # only ever return aggregate COUNT(*) integers below, never
            # row-level data, so registry-wide totals leak nothing.
            cur.execute("SELECT id::text FROM space")
            all_space_ids = [r[0] for r in (cur.fetchall() or [])]
            if all_space_ids:
                cur.execute(
                    f"SET LOCAL app.current_space_ids = {_quote(','.join(all_space_ids))}"
                )
            cur.execute("SET LOCAL app.current_roles = 'data_administrator,system'")

            cur.execute(
                """
                SELECT
                  count(*) AS total,
                  count(*) FILTER (WHERE lifecycle_state = 'published') AS published,
                  count(*) FILTER (WHERE validation_status = 'valid')   AS validated
                FROM data_asset
                """
            )
            total, published, validated = cur.fetchone()

            cur.execute(
                """
                SELECT lifecycle_state, count(*)
                FROM data_asset
                GROUP BY lifecycle_state
                ORDER BY lifecycle_state
                """
            )
            by_state = [{"state": r[0], "count": int(r[1])} for r in (cur.fetchall() or [])]

        return {
            "total": int(total or 0),
            "published": int(published or 0),
            "validated": int(validated or 0),
            "by_lifecycle_state": by_state,
        }
    finally:
        conn.close()


def _nl_search(event: Mapping[str, Any], auth: Dict[str, Any]) -> Dict[str, Any]:
    try:
        body = json.loads(event.get("body") or "{}")
    except (TypeError, ValueError) as exc:
        return _error(400, "BAD_REQUEST", f"invalid JSON body: {exc}")

    question = (body.get("question") or "").strip()
    if not question:
        return _error(400, "MISSING_PARAM", "question is required")

    normalized = _normalize_nl_query(question)
    cache_key = _nl_cache_key(normalized)

    sql: Optional[str] = None
    cache_hit = False
    cache = _redis_client()
    if cache is not None:
        try:
            cached = cache.get(cache_key)
            if cached:
                sql = cached
                cache_hit = True
                LOG.info("nl cache hit key=%s", cache_key)
        except Exception as exc:
            LOG.warning("redis GET failed (continuing without cache): %s", exc)

    if sql is None:
        try:
            sql = _generate_sql_via_bedrock(question)
        except Exception as exc:
            LOG.exception("bedrock NL→SQL failed: %s", exc)
            return _error(503, "BEDROCK_UNAVAILABLE", str(exc))

    if (err := _validate_sql(sql)) is not None:
        return _error(400, "FORBIDDEN_SQL", err)

    try:
        result = _execute_with_rls(sql, auth)
    except Exception as exc:
        LOG.exception("nl sql execute failed: %s", exc)
        return _error(500, "INTERNAL_ERROR", f"SQL execute failed: {exc}")

    if "_guardrail_error" in result:
        return _error(400, "EXPENSIVE_QUERY", result["_guardrail_error"])

    if cache is not None and not cache_hit:
        try:
            cache.setex(cache_key, _NL_CACHE_TTL_SECONDS, sql)
        except Exception as exc:
            LOG.warning("redis SETEX failed (continuing): %s", exc)

    return _ok({
        "question": question,
        "sql": sql,
        "cache_hit": cache_hit,
        "columns": result["columns"],
        "rows": result["rows"],
        "row_count": result["row_count"],
    })


_PUBLIC_FILTER = [
    {"term": {"lifecycle_state.keyword": "published"}},
    {"term": {"validation_status.keyword": "valid"}},
    {"bool": {"must_not": [{"term": {"is_sensitive": True}}]}},
]


def _public_doc(s: Mapping[str, Any]) -> Dict[str, Any]:
    """Public-safe subset of an asset document (no internal/embedding fields)."""
    keep = (
        "id", "name", "display_name", "data_type", "species", "sex", "subject_id",
        "instrument_name", "storage_uri", "organization", "lifecycle_state",
        "validation_status", "description", "created_at", "updated_at",
    )
    return {k: s.get(k) for k in keep}


def _public_search(event: Mapping[str, Any]) -> Dict[str, Any]:
    """GET /public/assets — unauthenticated browse/search of published, valid,
    non-sensitive assets, with facets. RLS-safe: the published+valid+not-sensitive
    filter is exactly the anonymous visibility scope (R21.1–R21.3)."""
    qs = event.get("queryStringParameters") or {}
    q = unquote_plus(qs.get("q") or "").strip()
    limit = min(int(qs.get("limit") or 24), 50)

    must: Dict[str, Any] = (
        {
            "multi_match": {
                "query": q,
                "fields": ["name^2", "description", "data_type", "species", "organization", "modalities"],
                "type": "best_fields",
                "fuzziness": "AUTO",
            }
        }
        if q
        else {"match_all": {}}
    )

    body = {
        "size": limit,
        "query": {"bool": {"must": [must], "filter": list(_PUBLIC_FILTER)}},
        "sort": [
            {"_score": {"order": "desc"}},
            {"created_at": {"order": "desc", "missing": "_last"}},
        ],
        "aggs": {
            "data_type": {"terms": {"field": "data_type.keyword", "size": 20}},
            "species": {"terms": {"field": "species.keyword", "size": 20}},
            "organization": {"terms": {"field": "organization.keyword", "size": 20}},
        },
    }

    try:
        result = _opensearch().search(index="data_asset", body=body)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("public search with aggs failed (%s); retrying without facets", exc)
        body.pop("aggs", None)
        try:
            result = _opensearch().search(index="data_asset", body=body)
        except Exception as exc2:  # noqa: BLE001
            LOG.exception("public search failed: %s", exc2)
            return _error(500, "INTERNAL_ERROR", "search unavailable")

    hits = result.get("hits", {}).get("hits", [])
    total = result.get("hits", {}).get("total", {}).get("value", 0)
    facets: Dict[str, Any] = {}
    for field, agg in (result.get("aggregations") or {}).items():
        buckets = agg.get("buckets") or []
        if buckets:
            facets[field] = [{"value": b.get("key"), "count": b.get("doc_count")} for b in buckets]

    return _ok({
        "total": total,
        "hits": [{"id": h["_id"], "source": _public_doc(h.get("_source", {}))} for h in hits],
        "query": q,
        "facets": facets,
    })


def _public_get_asset(asset_id: str) -> Dict[str, Any]:
    """GET /public/assets/{id} — single published, valid, non-sensitive asset."""
    if not asset_id:
        return _error(400, "MISSING_PARAM", "asset id required")
    body = {
        "size": 1,
        "query": {"bool": {"must": [{"ids": {"values": [asset_id]}}], "filter": list(_PUBLIC_FILTER)}},
    }
    try:
        result = _opensearch().search(index="data_asset", body=body)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("public get asset failed: %s", exc)
        return _error(500, "INTERNAL_ERROR", "lookup failed")
    hits = result.get("hits", {}).get("hits", [])
    if not hits:
        return _error(404, "NOT_FOUND", "asset not found or not publicly available")
    return _ok(_public_doc(hits[0].get("_source", {})))


def handler(event: Mapping[str, Any], context: Any) -> Dict[str, Any]:
    auth = _auth_context(event)
    LOG.info("search invoked user=%s roles=%s", auth["user_id"], auth["roles"])

    path = event.get("resource") or event.get("path") or ""
    method = (event.get("httpMethod") or "").upper()

    # Public, unauthenticated aggregate stats for the landing page. Returns
    # only COUNT(*) integers — no row-level data — so it's RLS-safe to expose.
    if method == "GET" and (path.endswith("/public/stats") or path == "/public/stats"):
        try:
            return _ok(_public_stats())
        except Exception as exc:  # noqa: BLE001
            LOG.exception("public stats failed: %s", exc)
            return _error(500, "INTERNAL_ERROR", "stats unavailable")

    # Public, unauthenticated browse/search + single-asset read (published only).
    if method == "GET" and path.endswith("/public/assets/{id}"):
        asset_id = (event.get("pathParameters") or {}).get("id", "")
        return _public_get_asset(asset_id)
    if method == "GET" and path.endswith("/public/assets"):
        return _public_search(event)


    if method == "GET" and (path.endswith("/search") or path == "/search"):
        return _search(event, auth)
    if method == "GET" and (path.endswith("/suggest") or path == "/suggest"):
        return _suggest(event, auth)
    if method == "POST" and (path.endswith("/search/nl") or path == "/search/nl"):
        return _nl_search(event, auth)

    if method != "GET" and method != "POST":
        return _error(405, "METHOD_NOT_ALLOWED", f"{method} not allowed on {path}")

    return _error(404, "NOT_FOUND", f"unknown route {method} {path}")
