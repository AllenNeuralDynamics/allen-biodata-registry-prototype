"""Diagnostic: print revision counts under admin RLS context for given asset IDs.

This script is intended to be uploaded as a one-off Lambda or run from a
bastion. For local use it requires direct VPC connectivity to Aurora.
"""
import boto3
import psycopg
import sys


def main(asset_ids):
    rds = boto3.client("rds", region_name="us-west-2")
    host = "biodata-registry-dev-aurora.cluster-c3a02eumwrfy.us-west-2.rds.amazonaws.com"
    user = "biodata_app"
    db = "biodata_registry"
    token = rds.generate_db_auth_token(
        DBHostname=host, Port=5432, DBUsername=user, Region="us-west-2"
    )

    conn = psycopg.connect(
        host=host, port=5432, user=user, password=token, dbname=db,
        sslmode="require", connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SET LOCAL app.current_user_id = 'bbe67b9c-06b0-4a65-9522-1f1eea72c426'"
            )
            cur.execute(
                "SET LOCAL app.current_roles = 'data_administrator,org_admin,space_admin'"
            )
            cur.execute(
                "SET LOCAL app.current_org_ids = 'a17216c5-7adc-41d9-8880-6841e6f57585'"
            )
            cur.execute(
                "SET LOCAL app.current_space_ids = '85cb4705-8159-4244-81bd-52c81565b406'"
            )

            for aid in asset_ids:
                cur.execute(
                    "SELECT id, name, sensitive_flag, space_id, lifecycle_state "
                    "FROM data_asset WHERE id = %s",
                    (aid,),
                )
                asset = cur.fetchone()
                cur.execute(
                    "SELECT count(*) FROM entity_revision "
                    "WHERE entity_type='data_asset' AND entity_id = %s",
                    (aid,),
                )
                count = cur.fetchone()[0]
                print(
                    f"asset={aid} found={asset is not None} "
                    f"sensitive_flag={asset[2] if asset else 'N/A'} "
                    f"lifecycle={asset[4] if asset else 'N/A'} "
                    f"revision_count={count}"
                )
    finally:
        conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
