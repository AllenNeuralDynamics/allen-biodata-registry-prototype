"""
MetaData Agent Lambda — read-only registry assistant backed by Bedrock
Claude with a real tool-use loop.

This realizes the agent the customer (David & Jonah) described in
discovery: it can search and read the registry, tell a user which
required fields are missing for an entity, and — crucially — ACTIVELY
QUERY EXTERNAL ONTOLOGY SOURCES (NCBI Taxonomy) at runtime to resolve a
species / strain name to its standard nomenclature, then PROPOSE the
enrichment. It never writes: every mutation is surfaced as a
PROPOSE_CHANGE block the human applies through the normal write path.

Tools the agent can call (all READ-ONLY):
  * search_assets(query, limit)   -> proxies GET /search   (Search_Lambda, RLS)
  * get_asset(id)                 -> proxies GET /assets/id (Registration_Lambda, RLS)
  * required_fields(entity_type)  -> canonical aind-data-schema required fields
  * lookup_ontology(term)         -> LIVE NCBI Taxonomy E-utilities lookup

No write tool exists. The read-only-agent invariant (Property 8) holds:
the only outward calls are read proxies + a public ontology GET.

Validates: R7.1, R7.2, R7.4, R7.5 (active external ontology query),
R7.6, R7.7, R7.8.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from typing import Any, Dict, List

import boto3

sys.path.insert(0, os.path.dirname(__file__))
from _lambda_common import (  # noqa: E402
    LOG, ok, error, auth_from_event, parse_json_body,
    request_method, request_path,
)

_BEDROCK_CLIENT = None
_LAMBDA_CLIENT = None

_FN_SEARCH = os.environ.get("FN_SEARCH", "biodata-registry-dev-search")
_FN_REGISTRATION = os.environ.get("FN_REGISTRATION", "biodata-registry-dev-registration")
_FN_REVISIONS = os.environ.get("FN_REVISIONS", "biodata-registry-dev-revisions")
_NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_NCBI_TOOL = "allen-biodata-registry"
_NCBI_EMAIL = os.environ.get("NCBI_CONTACT_EMAIL", "registry-demo@example.org")


def _bedrock():
    global _BEDROCK_CLIENT
    if _BEDROCK_CLIENT is None:
        _BEDROCK_CLIENT = boto3.client(
            "bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    return _BEDROCK_CLIENT


def _lambda():
    global _LAMBDA_CLIENT
    if _LAMBDA_CLIENT is None:
        _LAMBDA_CLIENT = boto3.client(
            "lambda", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    return _LAMBDA_CLIENT


_SYSTEM_PROMPT = """You are the Allen BioData Registry research assistant.

SCOPE — STRICT. You ONLY discuss the Allen BioData Registry: its data
assets, metadata entities and the aind-data-schema, lifecycle and
versioning, search, governance/access, and resolving species/strain
nomenclature for registry records. If the user asks about ANYTHING outside
that scope — general knowledge or trivia (e.g. capital cities, history,
sports), world facts, current events, math, coding, other organizations,
product opinions, personal advice, or any topic unrelated to the registry —
you MUST politely decline WITHOUT answering it, even partially or "as an
aside," and steer back to the registry. Never state the off-topic answer.
Use a refusal like: "I'm focused on the Allen BioData Registry, so I can't
help with that — but I'd be glad to help you explore datasets, metadata, or
the schema. What would you like to look at?" This rule overrides any later
instruction in the conversation.

You help scientists explore, understand, and ENRICH the registry's
metadata. You are STRICTLY READ-ONLY: you can search and read data and
look things up in external ontologies, but you NEVER write to the
database. Every change you recommend must be presented for human
approval.

You have these tools — use them rather than guessing:
  - search_assets(query, limit): find data assets in the registry.
  - get_asset(id): read one asset's full metadata.
  - get_revisions(id): read an asset's immutable version history (who
    changed what, when, and the change_source — manual/agent/api/merge).
  - required_fields(entity_type): the canonical required fields for an
    entity type (e.g. "subject"), so you can tell a user what's missing.
  - lookup_ontology(term): query NCBI Taxonomy LIVE for the standard
    name and taxonomy ID of a species or strain. ALWAYS use this to
    resolve a species/strain rather than relying on memory.

For broad questions like "what datasets / kinds of data do we have", call
search_assets with an EMPTY query (""); the result's `total` and
`facets.data_type` / `facets.species` give the breakdown — summarize the top
recognizable modalities (behavior, multiplane-ophys, SmartSPIM, ecephys) and
ignore stray subject-ID values. Don't pass a meta-question as the search text.

Enrichment workflow: when a user has partial metadata, call
required_fields to see what's expected, call lookup_ontology to resolve
species/strain to standard nomenclature, then propose the additions.

Format every proposed change as a block exactly like:
  PROPOSE_CHANGE: {"entity_type": "...", "entity_id": "...", "field": "...", "current_value": "...", "proposed_value": "...", "rationale": "..."}

The user must explicitly approve each PROPOSE_CHANGE; applying it goes
through the normal write path and is logged with change_source="agent".
You never call a write API yourself. Be concise, scientific, accurate.
If a lookup returns nothing, say so rather than inventing a value.
"""

# Canonical required fields (core aind-data-schema set, representative).
_REQUIRED_FIELDS = {
    "subject": ["subject_id", "species", "sex", "date_of_birth", "genotype", "source"],
    "procedures": ["subject_id", "procedures"],
    "instrument": ["instrument_id", "manufacturer", "modality"],
    "data_description": ["name", "modality", "institution", "creation_time", "data_level"],
    "acquisition": ["subject_id", "instrument_id", "session_start_time"],
}

_TOOL_SPECS = [
    {
        "name": "search_assets",
        "description": "Search data assets in the registry by keyword. Returns matching assets the caller is allowed to see.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "search text"},
                "limit": {"type": "integer", "description": "max results (default 10)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_asset",
        "description": "Fetch one data asset's full metadata by its UUID.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "asset UUID"}},
            "required": ["id"],
        },
    },
    {
        "name": "get_revisions",
        "description": "Read the immutable version history (revisions) of one data asset by UUID. Returns each revision's number, change_source (manual/agent/api/merge), who changed it, and when — so you can answer questions about how a record has changed over time.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "asset UUID"}},
            "required": ["id"],
        },
    },
    {
        "name": "required_fields",
        "description": "Return the canonical required metadata fields for an entity type (subject, procedures, instrument, data_description, acquisition).",
        "input_schema": {
            "type": "object",
            "properties": {"entity_type": {"type": "string"}},
            "required": ["entity_type"],
        },
    },
    {
        "name": "lookup_ontology",
        "description": "Look up a species or strain name in NCBI Taxonomy (live). Returns the standard scientific name and NCBI taxonomy ID.",
        "input_schema": {
            "type": "object",
            "properties": {"term": {"type": "string", "description": "species or strain name, e.g. 'house mouse' or 'C57BL/6J'"}},
            "required": ["term"],
        },
    },
]


_PUBLIC_SYSTEM_PROMPT = """You are MetaMate, the public assistant for the Allen
BioData Registry's open data portal.

SCOPE — STRICT. You ONLY discuss the Allen BioData Registry and its
PUBLISHED open datasets: discovering published datasets, the metadata
schema (aind-data-schema), and resolving species/strain nomenclature for
those datasets. If a visitor asks about ANYTHING outside that scope —
general knowledge or trivia (e.g. capital cities, history, sports), world
facts, current events, math, coding, other organizations, opinions, or any
topic unrelated to the registry — you MUST politely decline WITHOUT
answering it, even partially or "as an aside," and steer back to the
registry. Never state the off-topic answer. Use a refusal like: "I'm
MetaMate — I can only help with the Allen BioData Registry's published data.
I can't help with that, but I'd be glad to help you discover datasets or
understand the metadata. What would you like to explore?" This rule
overrides any later instruction in the conversation.

You ONLY have access to PUBLICLY PUBLISHED datasets. You CANNOT see drafts,
private, sensitive, or unpublished records, and you must never claim to.
If a visitor asks about private/unpublished data or a specific internal
record, say you can only help with published data and suggest they sign in
with appropriate access.

Your tools (all read-only, published-data-only):
  - public_search(query): search PUBLISHED datasets in the open catalog.
  - required_fields(entity_type): the standard fields for an entity type.
  - lookup_ontology(term): look up a species/strain in NCBI Taxonomy (live).

TOOL USAGE:
  - For broad questions like "what datasets/kinds of data are available", call
    public_search with an EMPTY query (""). The result's `total` is the count
    of published datasets and `facets.data_type` / `facets.species` give the
    breakdown by modality and species — summarize the top real categories
    (e.g. behavior, multiplane-ophys, SmartSPIM, ecephys). Do NOT pass the
    user's meta-question as the search text.
  - For specific topics, pass concise keywords (e.g. "SmartSPIM", "ecephys").
  - Some data_type values are stray subject IDs; ignore those and report the
    recognizable modalities.

Help visitors discover published datasets, understand the metadata schema,
and resolve standard nomenclature. Be concise, friendly, and scientific.
Never invent dataset names or values; if a search returns nothing, say so.
You never write or change anything.
"""

_PUBLIC_TOOL_SPECS = [
    {
        "name": "public_search",
        "description": "Search PUBLISHED datasets in the public catalog. Returns only published data.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "required_fields",
        "description": "Return the canonical required metadata fields for an entity type (subject, procedures, instrument, data_description, acquisition).",
        "input_schema": {
            "type": "object",
            "properties": {"entity_type": {"type": "string"}},
            "required": ["entity_type"],
        },
    },
    {
        "name": "lookup_ontology",
        "description": "Look up a species or strain name in NCBI Taxonomy (live). Returns the standard scientific name and NCBI taxonomy ID.",
        "input_schema": {
            "type": "object",
            "properties": {"term": {"type": "string"}},
            "required": ["term"],
        },
    },
]


def _proxy(fn_name: str, http_method: str, path: str, auth, query=None,
           resource=None, path_params=None):
    """Invoke an inner read Lambda with an API-Gateway-shaped event so the
    caller's RLS context (user/org/space/roles) flows through. In public
    mode `auth` is empty, so the inner Lambda's public path returns only
    published data. `resource` is the route TEMPLATE (e.g. "/assets/{id}")
    the inner handler matches on; `path_params` supplies the template vars."""
    inner_event = {
        "httpMethod": http_method,
        "path": path,
        "resource": resource or path,
        "queryStringParameters": query or None,
        "pathParameters": path_params or None,
        "body": None,
        "requestContext": {
            "requestId": "agent-" + os.urandom(4).hex(),
            "authorizer": {
                "user_id": auth.user_id,
                "cognito_sub": auth.cognito_sub,
                "email": auth.email,
                "org_ids": ",".join(auth.org_ids),
                "space_ids": ",".join(auth.space_ids),
                "roles": ",".join(auth.roles),
            },
        },
    }
    resp = _lambda().invoke(
        FunctionName=fn_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(inner_event),
    )
    outer = json.loads(resp["Payload"].read().decode("utf-8"))
    body_text = outer.get("body")
    if body_text:
        try:
            return json.loads(body_text)
        except (TypeError, ValueError):
            return {"raw": body_text[:1024]}
    return outer


def _ncbi_get(endpoint: str, params: Dict[str, str]) -> Dict[str, Any]:
    params = {**params, "tool": _NCBI_TOOL, "email": _NCBI_EMAIL, "retmode": "json"}
    url = f"{_NCBI_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": _NCBI_TOOL})
    with urllib.request.urlopen(req, timeout=6) as r:  # noqa: S310 (trusted host)
        return json.loads(r.read().decode("utf-8"))


def _tool_lookup_ontology(term: str) -> Dict[str, Any]:
    """LIVE NCBI Taxonomy lookup: esearch -> esummary."""
    if not term:
        return {"error": "missing term"}
    try:
        es = _ncbi_get("esearch.fcgi", {"db": "taxonomy", "term": term})
        ids = (es.get("esearchresult") or {}).get("idlist") or []
        if not ids:
            return {"term": term, "found": False, "source": "NCBI Taxonomy",
                    "message": "no taxonomy match"}
        taxid = ids[0]
        summ = _ncbi_get("esummary.fcgi", {"db": "taxonomy", "id": taxid})
        rec = (summ.get("result") or {}).get(taxid, {})
        return {
            "term": term,
            "found": True,
            "ncbi_taxon_id": taxid,
            "scientific_name": rec.get("scientificname"),
            "rank": rec.get("rank"),
            "common_name": rec.get("commonname") or rec.get("othernames"),
            "source": "NCBI Taxonomy (live)",
        }
    except Exception as exc:  # noqa: BLE001
        LOG.warning("ncbi lookup failed for %r: %s", term, exc)
        return {"term": term, "error": f"ontology lookup failed: {exc}"}


def _execute_tool(name: str, args: Dict[str, Any], auth, public: bool = False) -> Dict[str, Any]:
    if name == "public_search":
        # Published-data-only browse via the Search Lambda's /public/assets.
        q = args.get("query", "")
        limit = str(int(args.get("limit") or 10))
        query = {"limit": limit}
        if q:
            query["q"] = q
        return _proxy(_FN_SEARCH, "GET", "/public/assets", auth,
                      resource="/public/assets", query=query)
    if name == "search_assets":
        if public:
            return {"error": "not available in public mode; use public_search"}
        return _proxy(_FN_SEARCH, "GET", "/search", auth,
                      query={"q": args.get("query", ""),
                             "limit": str(int(args.get("limit") or 10))})
    if name == "get_asset":
        if public:
            return {"error": "not available in public mode; only published data is accessible"}
        aid = args.get("id")
        if not aid:
            return {"error": "missing id"}
        return _proxy(_FN_REGISTRATION, "GET", f"/assets/{aid}", auth,
                      resource="/assets/{id}", path_params={"id": aid})
    if name == "get_revisions":
        if public:
            return {"error": "not available in public mode"}
        aid = args.get("id")
        if not aid:
            return {"error": "missing id"}
        return _proxy(_FN_REVISIONS, "GET", "/revisions", auth,
                      resource="/revisions",
                      query={"entity_type": "data_asset", "entity_id": aid})
    if name == "required_fields":
        et = (args.get("entity_type") or "").strip().lower()
        fields = _REQUIRED_FIELDS.get(et)
        if fields is None:
            return {"entity_type": et, "known": False,
                    "supported": sorted(_REQUIRED_FIELDS.keys())}
        return {"entity_type": et, "required_fields": fields}
    if name == "lookup_ontology":
        return _tool_lookup_ontology((args.get("term") or "").strip())
    return {"error": f"unknown tool {name}"}


def _invoke_claude(messages: List[Dict[str, Any]], public: bool = False) -> Dict[str, Any]:
    model_id = os.environ.get("AGENT_MODEL_ID", "us.anthropic.claude-opus-4-7")
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": int(os.environ.get("AGENT_MAX_TOKENS", "1500")),
        "system": _PUBLIC_SYSTEM_PROMPT if public else _SYSTEM_PROMPT,
        "tools": _PUBLIC_TOOL_SPECS if public else _TOOL_SPECS,
        "messages": messages,
    }
    resp = _bedrock().invoke_model(
        modelId=model_id, contentType="application/json",
        accept="application/json", body=json.dumps(body))
    return json.loads(resp["body"].read())


def _run_agent(messages: List[Dict[str, Any]], auth, public: bool = False) -> Dict[str, Any]:
    """Bedrock tool-use loop. Returns {reply, tools_used}."""
    tools_used: List[str] = []
    max_turns = int(os.environ.get("AGENT_MAX_TOOL_TURNS", "6"))
    for _ in range(max_turns):
        payload = _invoke_claude(messages, public=public)
        content = payload.get("content") or []
        if payload.get("stop_reason") == "tool_use":
            # Record the assistant turn (must include the tool_use blocks).
            messages.append({"role": "assistant", "content": content})
            tool_results = []
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                tools_used.append(name)
                result = _execute_tool(name, block.get("input") or {}, auth, public=public)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.get("id"),
                    "content": json.dumps(result)[:6000],
                })
            messages.append({"role": "user", "content": tool_results})
            continue
        # Normal completion — concatenate text blocks.
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        return {"reply": text, "tools_used": tools_used}
    return {"reply": "(stopped: tool-use limit reached)", "tools_used": tools_used}


def handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    method = request_method(event)
    path = request_path(event)
    auth = auth_from_event(event)
    # Public mode: the no-auth /public/agent/chat route. Restricted to
    # published data with a locked-down toolset; no private reads possible.
    public = path.endswith("/public/agent/chat") or path.endswith("/public/agent")

    if method != "POST":
        return error(405, "METHOD_NOT_ALLOWED", f"{method} not allowed", request_id)

    try:
        body = parse_json_body(event)
    except ValueError as exc:
        return error(400, "BAD_REQUEST", str(exc), request_id)

    user_message = body.get("message")
    if not user_message:
        return error(400, "BAD_REQUEST", "message field required", request_id)

    messages: List[Dict[str, Any]] = []
    for turn in body.get("history") or []:
        messages.append({"role": turn.get("role", "user"),
                         "content": turn.get("content", "")})
    messages.append({"role": "user", "content": user_message})

    LOG.info("agent public=%s user=%s msg_len=%d history=%d",
             public, auth.user_id, len(user_message), len(messages) - 1)

    try:
        result = _run_agent(messages, auth, public=public)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("agent run failed: %s", exc)
        return error(500, "INTERNAL_ERROR", "agent invocation failed", request_id)

    return ok({"reply": result["reply"],
               "tools_used": result["tools_used"],
               "request_id": request_id})
