"""
External MCP Server Lambda — exposes 7 read-only tools to external
agents over the Model Context Protocol.

Tools:
  1. search_assets         — fronted by Search_Lambda
  2. get_asset             — fronted by Registration_Lambda (read path)
  3. get_entity            — fronted by Registration_Lambda
  4. list_collections      — fronted by Collections_Lambda (read path)
  5. get_validation_status — fronted by Validation_Lambda
  6. aggregate_metadata    — fronted by Observability_Lambda
  7. explore_schema        — pure read, returns schema_definition rows

Trust model: every invocation goes through API Gateway with the
caller's Cognito JWT. The Authorizer Lambda resolves
{user_id, org_ids, space_ids, roles}; this MCP handler forwards that
same context to the inner Lambdas, so RLS context flows end-to-end.

Each tool's response is shaped per the MCP protocol:
  {
    "tool":   "<tool_name>",
    "result": <tool-specific JSON>,
    "ok":     true | false,
    "error":  null | {"code": "...", "message": "..."}
  }

Validates: R16.1, R16.2, R16.3, R16.4, R16.5, R16.6 | Design:
§External Interfaces.MCP Server (external).
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Mapping, Optional

import boto3

sys.path.insert(0, os.path.dirname(__file__))
from _lambda_common import (  # noqa: E402
    LOG, ok, error, AuthContext, aurora_connect,
    auth_from_event, parse_json_body,
    request_path, request_method,
)


_LAMBDA_CLIENT = None


def _lambda():
    global _LAMBDA_CLIENT
    if _LAMBDA_CLIENT is None:
        _LAMBDA_CLIENT = boto3.client(
            "lambda", region_name=os.environ.get("AWS_REGION", "us-west-2")
        )
    return _LAMBDA_CLIENT


# Lambda function names are read from env vars so the Terraform wiring
# can supply the actual deployed names. Defaults are the dev-environment
# names so a sane response is produced even before TF rollout.
_FN = {
    "search":         os.environ.get("FN_SEARCH",         "biodata-registry-dev-search"),
    "registration":   os.environ.get("FN_REGISTRATION",   "biodata-registry-dev-registration"),
    "collections":    os.environ.get("FN_COLLECTIONS",    "biodata-registry-dev-collections"),
    "validation":     os.environ.get("FN_VALIDATION",     "biodata-registry-dev-validation"),
    "observability":  os.environ.get("FN_OBSERVABILITY",  "biodata-registry-dev-observability"),
}


# ---------------------------------------------------------------------------
# Tool implementations.
#
# Each tool function takes the parsed args + auth context, invokes an inner
# Lambda (or queries Aurora directly), and returns the inner result. We
# preserve the auth's authorizer-context fields when invoking the inner
# Lambda so RLS flows through.
# ---------------------------------------------------------------------------

def _proxy(fn_key: str, http_method: str, path: str, auth: AuthContext,
           query: Optional[Dict[str, Any]] = None,
           body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Synchronously invoke an inner Lambda with an API-Gateway-shaped
    event. Returns the parsed JSON body of the response."""
    inner_event = {
        "httpMethod": http_method,
        "path": path,
        "resource": path,
        "queryStringParameters": query or None,
        "pathParameters": None,
        "body": json.dumps(body) if body else None,
        "requestContext": {
            "requestId": "mcp-" + os.urandom(4).hex(),
            "authorizer": {
                "user_id":   auth.user_id,
                "email":     auth.email,
                "org_ids":   ",".join(auth.org_ids),
                "space_ids": ",".join(auth.space_ids),
                "roles":     ",".join(auth.roles),
            },
        },
    }
    response = _lambda().invoke(
        FunctionName=_FN[fn_key],
        InvocationType="RequestResponse",
        Payload=json.dumps(inner_event),
    )
    payload = response["Payload"].read().decode("utf-8")
    try:
        outer = json.loads(payload)
    except (TypeError, ValueError):
        return {"error": "invalid response from inner Lambda", "raw": payload[:512]}
    body_text = outer.get("body")
    if body_text:
        try:
            return json.loads(body_text)
        except (TypeError, ValueError):
            return {"raw": body_text[:1024]}
    return outer


def tool_search_assets(args: Dict[str, Any], auth: AuthContext) -> Dict[str, Any]:
    q = args.get("query") or args.get("q") or ""
    limit = int(args.get("limit") or 20)
    return _proxy("search", "GET", "/search", auth, query={"q": q, "limit": str(limit)})


def tool_get_asset(args: Dict[str, Any], auth: AuthContext) -> Dict[str, Any]:
    asset_id = args.get("id")
    if not asset_id:
        return {"error": "missing required arg: id"}
    return _proxy(
        "registration", "GET", f"/assets/{asset_id}", auth,
    )


def tool_get_entity(args: Dict[str, Any], auth: AuthContext) -> Dict[str, Any]:
    entity_type = args.get("entity_type")
    entity_id = args.get("id")
    if not (entity_type and entity_id):
        return {"error": "missing required args: entity_type, id"}
    return _proxy(
        "registration", "GET", f"/entities/{entity_type}/{entity_id}", auth,
    )


def tool_list_collections(args: Dict[str, Any], auth: AuthContext) -> Dict[str, Any]:
    limit = int(args.get("limit") or 50)
    return _proxy(
        "collections", "GET", "/collections", auth,
        query={"limit": str(limit)},
    )


def tool_get_validation_status(args: Dict[str, Any], auth: AuthContext) -> Dict[str, Any]:
    """Returns the current validation_status for one asset by querying
    Aurora directly under RLS context. We don't proxy Validation_Lambda
    here because it's a write-trigger; the read path is a single
    SELECT."""
    asset_id = args.get("id")
    if not asset_id:
        return {"error": "missing required arg: id"}
    conn = aurora_connect(auth)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, validation_status, validation_errors "
                "FROM data_asset WHERE id = %s",
                (asset_id,),
            )
            row = cur.fetchone()
            if row is None:
                return {"error": "not found"}
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, [str(v) if v is not None else None for v in row]))
    finally:
        conn.close()


def tool_aggregate_metadata(args: Dict[str, Any], auth: AuthContext) -> Dict[str, Any]:
    """Aggregate metrics — fan out to the observability lambda."""
    metric = args.get("metric") or "asset-counts"
    return _proxy(
        "observability", "GET", f"/metrics/{metric}", auth,
    )


def tool_explore_schema(args: Dict[str, Any], auth: AuthContext) -> Dict[str, Any]:
    """Return the registered Custom_Schemas under RLS context."""
    conn = aurora_connect(auth)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, version, schema_kind, is_active, deprecated_at, created_at "
                "FROM schema_definition ORDER BY name, version DESC LIMIT 200"
            )
            cols = [d[0] for d in cur.description]
            return {
                "schemas": [
                    {c: (str(v) if v is not None else None) for c, v in zip(cols, row)}
                    for row in cur.fetchall()
                ]
            }
    finally:
        conn.close()


_TOOLS = {
    "search_assets":         tool_search_assets,
    "get_asset":             tool_get_asset,
    "get_entity":            tool_get_entity,
    "list_collections":      tool_list_collections,
    "get_validation_status": tool_get_validation_status,
    "aggregate_metadata":    tool_aggregate_metadata,
    "explore_schema":        tool_explore_schema,
}


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

def handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    method = request_method(event)
    path = request_path(event)
    auth = auth_from_event(event)

    # GET /mcp/tools — discovery: list all available tools and arg shapes.
    if method == "GET" and (path.endswith("/mcp/tools") or path == "/mcp/tools"):
        return ok({
            "tools": [
                {"name": "search_assets",
                 "description": "BM25 + facet search over data_asset",
                 "args": {"query": "str", "limit": "int (optional)"}},
                {"name": "get_asset",
                 "description": "Fetch a single data_asset by id",
                 "args": {"id": "uuid"}},
                {"name": "get_entity",
                 "description": "Fetch a shared entity (subject/instrument/etc.)",
                 "args": {"entity_type": "str", "id": "uuid"}},
                {"name": "list_collections",
                 "description": "List collections visible to caller",
                 "args": {"limit": "int (optional)"}},
                {"name": "get_validation_status",
                 "description": "Current validation status + errors for one asset",
                 "args": {"id": "uuid"}},
                {"name": "aggregate_metadata",
                 "description": "Registry-wide metrics (asset-counts, validation-distribution, growth)",
                 "args": {"metric": "asset-counts | validation-distribution | growth"}},
                {"name": "explore_schema",
                 "description": "List Custom_Schemas registered in the registry",
                 "args": {}},
            ]
        })

    # POST /mcp/invoke — invoke a tool.
    if method == "POST" and (path.endswith("/mcp/invoke") or path == "/mcp/invoke"):
        try:
            body = parse_json_body(event)
        except ValueError as exc:
            return error(400, "BAD_REQUEST", str(exc), request_id)

        tool_name = body.get("tool")
        args = body.get("args") or {}
        if tool_name not in _TOOLS:
            return error(
                400, "UNKNOWN_TOOL",
                f"unknown tool {tool_name!r}; available: {sorted(_TOOLS.keys())}",
                request_id,
            )
        try:
            result = _TOOLS[tool_name](args, auth)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("mcp tool %s failed: %s", tool_name, exc)
            return error(500, "TOOL_FAILED", str(exc), request_id)

        return ok({
            "tool":   tool_name,
            "result": result,
            "ok":     "error" not in result,
            "error":  ({"code": "TOOL_ERROR", "message": result["error"]}
                       if "error" in result else None),
        })

    return error(404, "NOT_FOUND", f"unknown route {method} {path}", request_id)
