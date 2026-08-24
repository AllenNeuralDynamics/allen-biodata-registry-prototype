"""CDC Reader Lambda — PLACEHOLDER (Task 17.1).

This module is shipped as a placeholder so the cdc-pipeline Terraform
module can `terraform apply` end-to-end before the production CDC
Reader source tree (services/cdc-reader/) lands as part of Task 18.x.

The PRODUCTION handler must:

  1. Resolve the Aurora connection details from environment variables
     (DB_HOST, DB_PORT, DB_NAME, DB_USER, AURORA_RESOURCE_ID).
  2. Open a replication connection to Aurora using IAM-issued tokens
     (`boto3.client('rds').generate_db_auth_token`) — the execution
     role grants `rds-db:connect` for exactly one DB user.
  3. Attach to the logical replication slot (CDC_SLOT_NAME) using the
     pgoutput plugin and the publication CDC_PUBLICATION.
  4. For each WAL message the slot emits, send a FIFO message to SQS
     with:
        - MessageBody             = JSON({op, table, pk, lsn, before, after})
        - MessageGroupId          = sha256(table || ':' || pk_value)
        - MessageDeduplicationId  = lsn  (or rely on content-based dedup)
  5. Advance the slot's confirmed_flush_lsn so consumed events are not
     redelivered on the next invocation.

On every run the handler must:
  * Bound work by elapsed wall-clock time (timeout - 5s) so the next
    scheduled invocation is not blocked.
  * Cleanly close the replication connection on exit.
  * Emit a structured CloudWatch log line:
    {events_sent, lsn_advanced_to, elapsed_ms, errors}.

See design.md §Architecture.CDC Pipeline Architecture for the full
contract and design.md §Error Handling.Failure Domains for the
"CDC transport failure" recovery story.

NOTE: This placeholder has zero production dependencies. The Lambda
package therefore weighs in at a few KB until services/cdc-reader/
ships with `psycopg[binary]` + `boto3` pinned in requirements.txt.
"""

import logging
import os

LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def handler(event, context):
    """Placeholder. Logs a warning and exits cleanly.

    Returns a dict with `status="not_implemented"` so an operator
    invoking the Lambda manually (e.g. `aws lambda invoke ...`) gets a
    clear signal that the production handler hasn't shipped yet.
    """
    LOGGER.warning(
        "cdc-reader placeholder invoked; production handler not yet "
        "implemented. See terraform/modules/cdc-pipeline/README.md "
        "(Implementation Gap section) and Task 18.x."
    )
    return {
        "status": "not_implemented",
        "message": (
            "cdc-reader placeholder Lambda. The cdc-pipeline Terraform "
            "module shipped only the SQS + IAM scaffolding; the actual "
            "slot-drain handler lands with services/cdc-reader/."
        ),
        "slot_name": os.environ.get("CDC_SLOT_NAME", ""),
        "publication": os.environ.get("CDC_PUBLICATION", ""),
        "events_sent": 0,
    }
