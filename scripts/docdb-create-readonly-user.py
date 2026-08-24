#!/usr/bin/env python3
"""
docdb-create-readonly-user.py — one-shot bootstrap to create the
`docdb_readonly` user on the Allen BioData Registry DocumentDB cluster.

This is a template script — wired into Terraform via a null_resource in
**Task 8**. Until then, run it manually after `terraform apply` of the
`documentdb` module:

    pip install pymongo cryptography
    curl -sSLo /tmp/global-bundle.pem \\
        https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
    AWS_PROFILE=allen-poc python scripts/docdb-create-readonly-user.py \\
        --master-secret-arn  arn:aws:secretsmanager:us-west-2:<acct>:secret:biodata-registry-dev/docdb/master-credentials-XXXX \\
        --readonly-secret-arn arn:aws:secretsmanager:us-west-2:<acct>:secret:biodata-registry-dev/docdb/readonly-credentials-XXXX \\
        --database biodata_registry \\
        --tls-ca-file /tmp/global-bundle.pem

What it does:
    1. Fetches the master credential from Secrets Manager.
    2. Connects to DocumentDB over TLS.
    3. Generates a random password.
    4. Creates (or updates) the `docdb_readonly` user with `read` role on the
       target database. (`read` role allows find/aggregate/listCollections
       and disallows any mutation — exactly the read-only contract that
       backs the `aind-data-access-api` trust model.)
    5. Writes the new credential into the readonly Secrets Manager secret,
       overwriting the placeholder created by Terraform.

Idempotent: re-running rotates the read-only password and updates the
secret. Existing connections continue to work until they are reconnected.

Validates: design.md §Design Decisions.DocumentDB Access Model and Trust
Boundary (read-only DB user for aind-data-access-api consumers).
"""
from __future__ import annotations

import argparse
import json
import logging
import secrets
import string
import sys
from typing import Any

# These imports are runtime requirements; the file ships in the repo as a
# template and is not auto-imported by any other module.
try:
    import boto3  # type: ignore
    from pymongo import MongoClient  # type: ignore
    from pymongo.errors import OperationFailure  # type: ignore
except ImportError as exc:  # pragma: no cover — runtime dep, not test dep
    raise SystemExit(
        "Missing runtime deps. Install with: pip install boto3 pymongo cryptography"
    ) from exc


LOG = logging.getLogger("docdb-bootstrap")

READONLY_USERNAME = "docdb_readonly"
PASSWORD_LENGTH = 32
# DocumentDB rejects "/", "@", '"', and whitespace in passwords.
PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!#$%&*()-_=+[]{}<>:?"


def _generate_password() -> str:
    """Cryptographically secure random password compatible with DocDB rules."""
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def _fetch_secret(client: Any, secret_arn: str) -> dict[str, Any]:
    raw = client.get_secret_value(SecretId=secret_arn)["SecretString"]
    return json.loads(raw)


def _build_mongo_uri(host: str, port: int, username: str, password: str) -> str:
    """
    Build a TLS-enabled, replica-set-aware Mongo URI. The connection options
    mirror the recommended DocumentDB connect string from
    https://docs.aws.amazon.com/documentdb/latest/developerguide/connect_programmatically.html.
    """
    # urllib.parse.quote handles passwords with reserved chars without
    # accidentally double-encoding ASCII letters/digits.
    from urllib.parse import quote_plus

    return (
        f"mongodb://{quote_plus(username)}:{quote_plus(password)}@{host}:{port}/"
        "?tls=true"
        "&retryWrites=false"
        "&replicaSet=rs0"
        "&readPreference=secondaryPreferred"
    )


def _create_or_update_readonly_user(
    admin_client: MongoClient, database: str, password: str
) -> None:
    """
    Idempotently provision the read-only user on the target database. Uses
    DocumentDB's `createUser` / `updateUser` admin commands (the standard
    Mongo equivalents).
    """
    db = admin_client[database]

    try:
        # If the user exists this raises OperationFailure with code 51003
        # (user already exists). DocumentDB returns code 11000 in some
        # versions — treat any "already exists" error as the update path.
        db.command(
            "createUser",
            READONLY_USERNAME,
            pwd=password,
            roles=[{"role": "read", "db": database}],
        )
        LOG.info("Created user %s on database %s", READONLY_USERNAME, database)
    except OperationFailure as exc:
        if exc.code in (51003, 11000) or "already exists" in str(exc).lower():
            LOG.info(
                "User %s already exists on %s — rotating password",
                READONLY_USERNAME,
                database,
            )
            db.command(
                "updateUser",
                READONLY_USERNAME,
                pwd=password,
                roles=[{"role": "read", "db": database}],
            )
        else:
            raise


def _write_readonly_secret(
    sm_client: Any,
    readonly_secret_arn: str,
    master_secret: dict[str, Any],
    password: str,
) -> None:
    """Overwrite the placeholder readonly secret with the real credential."""
    payload = {
        "engine": "docdb",
        "host": master_secret["host"],
        "reader_host": master_secret["reader_host"],
        "port": master_secret["port"],
        "username": READONLY_USERNAME,
        "password": password,
        "dbClusterIdentifier": master_secret["dbClusterIdentifier"],
        "ssl": True,
        "sslCABundleUrl": master_secret.get(
            "sslCABundleUrl",
            "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem",
        ),
        "bootstrapStatus": "ready",
    }
    sm_client.put_secret_value(
        SecretId=readonly_secret_arn,
        SecretString=json.dumps(payload),
    )
    LOG.info("Wrote rotated read-only credential to %s", readonly_secret_arn)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-secret-arn", required=True)
    parser.add_argument("--readonly-secret-arn", required=True)
    parser.add_argument(
        "--database",
        default="biodata_registry",
        help="Database the read-only user is granted `read` on (default: biodata_registry).",
    )
    parser.add_argument(
        "--tls-ca-file",
        default="/tmp/global-bundle.pem",
        help="Path to the downloaded RDS/DocumentDB CA bundle.",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="AWS region. Defaults to the boto3 default chain.",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    sm = boto3.client("secretsmanager", region_name=args.region)
    master = _fetch_secret(sm, args.master_secret_arn)

    LOG.info(
        "Connecting to %s:%s as %s (TLS via %s)",
        master["host"],
        master["port"],
        master["username"],
        args.tls_ca_file,
    )

    uri = _build_mongo_uri(
        host=master["host"],
        port=master["port"],
        username=master["username"],
        password=master["password"],
    )

    # tlsCAFile is what makes pymongo verify the DocumentDB server cert
    # against the AWS-published CA bundle.
    client = MongoClient(uri, tlsCAFile=args.tls_ca_file)
    try:
        client.admin.command("ping")
        LOG.info("Connected. Cluster ping OK.")

        new_password = _generate_password()
        _create_or_update_readonly_user(client, args.database, new_password)
        _write_readonly_secret(sm, args.readonly_secret_arn, master, new_password)
        LOG.info("Bootstrap complete.")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
