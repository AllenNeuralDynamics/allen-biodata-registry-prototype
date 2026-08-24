"""
Governance Lambda — POST /orgs, POST /orgs/{id}/spaces,
PUT /orgs/{id}/users/{uid}/role, POST /orgs/{id}/sharing-grants.

Validates: R9.1-R9.7, R19.2, R20.5, R3.6.
"""
from __future__ import annotations

import os
import sys

import boto3

sys.path.insert(0, os.path.dirname(__file__))
from _lambda_common import (  # noqa: E402
    LOG, ok, error, auth_from_event, aurora_connect, parse_json_body,
    request_path, request_method, path_param,
)


_SNS_TOPIC_PREFIX = os.environ.get(
    "SNS_TOPIC_PREFIX", "biodata-registry-dev-notifications-"
)
_SNS_CLIENT = None


def _sns():
    global _SNS_CLIENT
    if _SNS_CLIENT is None:
        _SNS_CLIENT = boto3.client(
            "sns", region_name=os.environ.get("AWS_REGION", "us-west-2")
        )
    return _SNS_CLIENT


def _ensure_org_topic(org_id: str, org_name: str, admin_email: str | None = None) -> str:
    """Create (idempotently) the per-Org SNS topic and optionally subscribe
    the admin email. Returns the topic ARN."""
    topic_name = f"{_SNS_TOPIC_PREFIX}{org_id}"
    resp = _sns().create_topic(
        Name=topic_name,
        Attributes={"KmsMasterKeyId": "alias/aws/sns"},
        Tags=[
            {"Key": "Project", "Value": "biodata-registry"},
            {"Key": "OrgId", "Value": str(org_id)},
            {"Key": "OrgName", "Value": str(org_name)[:128]},
        ],
    )
    topic_arn = resp["TopicArn"]

    if admin_email:
        try:
            _sns().subscribe(
                TopicArn=topic_arn,
                Protocol="email",
                Endpoint=admin_email,
                ReturnSubscriptionArn=True,
            )
            LOG.info("sns: subscribed %s to %s", admin_email, topic_arn)
        except Exception as exc:
            # Don't fail Org creation if subscribe fails — the email may
            # already be subscribed or pending confirmation.
            LOG.warning("sns: subscribe %s -> %s failed: %s", admin_email, topic_arn, exc)

    LOG.info("sns: ensured topic %s for org %s", topic_arn, org_id)
    return topic_arn


def _create_org(conn, auth, body, request_id):
    name = body.get("name")
    if not name:
        return error(400, "BAD_REQUEST", "name required", request_id)
    display_name = body.get("display_name") or name
    admin_email = body.get("admin_email")  # Optional — subscribed to the topic
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organization (name, display_name) VALUES (%s, %s) RETURNING id, name, display_name, created_at",
            (name, display_name),
        )
        row = cur.fetchone()
        conn.commit()
    cols = ["id", "name", "display_name", "created_at"]
    org = dict(zip(cols, row))

    # Best-effort SNS topic creation. If SNS fails the Org row is still
    # committed — operators can retry topic creation idempotently via
    # POST /orgs/{id}/notifications/topic (future work).
    try:
        topic_arn = _ensure_org_topic(str(org["id"]), str(org["name"]), admin_email)
        org["notification_topic_arn"] = topic_arn
    except Exception as exc:
        LOG.warning("sns: topic creation deferred for org %s: %s", org["id"], exc)
        org["notification_topic_arn"] = None

    return ok(org, status=201)


def _create_space(conn, auth, org_id, body, request_id):
    name = body.get("name")
    if not name:
        return error(400, "BAD_REQUEST", "name required", request_id)
    display_name = body.get("display_name") or name
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO space (org_id, name, display_name) VALUES (%s, %s, %s) RETURNING id, org_id, name, display_name, created_at",
            (org_id, name, display_name),
        )
        row = cur.fetchone()
        conn.commit()
    cols = ["id", "org_id", "name", "display_name", "created_at"]
    return ok(dict(zip(cols, row)), status=201)


def _set_user_role(conn, auth, org_id, user_id, body, request_id):
    role = body.get("role")
    if not role:
        return error(400, "BAD_REQUEST", "role required", request_id)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO user_org_role (user_id, org_id, role)
               VALUES (%s, %s, %s)
               ON CONFLICT (user_id, org_id, role) DO NOTHING""",
            (user_id, org_id, role),
        )
        conn.commit()
    return ok({"user_id": user_id, "org_id": org_id, "role": role})


def _create_sharing_grant(conn, auth, org_id, body, request_id):
    grantee_org_id = body.get("grantee_org_id")
    grantee_space_id = body.get("grantee_space_id")
    expires_at = body.get("expires_at")
    if not (grantee_org_id or grantee_space_id):
        return error(400, "BAD_REQUEST", "grantee_org_id or grantee_space_id required", request_id)

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO sharing_grant (granter_org_id, grantee_org_id, grantee_space_id, principal_org_id, role, expires_at, granted_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (org_id, grantee_org_id, grantee_space_id, org_id, body.get("role") or "viewer", expires_at, auth.user_id),
        )
        row = cur.fetchone()
        conn.commit()
    return ok({"id": str(row[0])}, status=201)


def handler(event, context):
    request_id = getattr(context, "aws_request_id", "unknown")
    method = request_method(event)
    path = request_path(event)
    auth = auth_from_event(event)

    try:
        body = parse_json_body(event)
    except ValueError as exc:
        return error(400, "BAD_REQUEST", str(exc), request_id)

    conn = aurora_connect(auth)
    try:
        # POST /orgs
        if method == "POST" and path == "/orgs":
            return _create_org(conn, auth, body, request_id)

        # POST /orgs/{id}/spaces
        if method == "POST" and path.endswith("/spaces"):
            org_id = path_param(event, "id")
            return _create_space(conn, auth, org_id, body, request_id)

        # PUT /orgs/{id}/users/{uid}/role
        if method == "PUT" and "/users/" in path and path.endswith("/role"):
            org_id = path_param(event, "id")
            user_id = path_param(event, "uid")
            return _set_user_role(conn, auth, org_id, user_id, body, request_id)

        # POST /orgs/{id}/sharing-grants
        if method == "POST" and path.endswith("/sharing-grants"):
            org_id = path_param(event, "id")
            return _create_sharing_grant(conn, auth, org_id, body, request_id)

        return error(404, "NOT_FOUND", f"unknown route {method} {path}", request_id)

    except Exception as exc:
        LOG.exception("governance failure: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return error(500, "INTERNAL_ERROR", "governance op failed", request_id)
    finally:
        conn.close()
