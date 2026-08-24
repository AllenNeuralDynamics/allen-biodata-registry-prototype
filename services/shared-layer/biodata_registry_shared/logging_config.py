"""
Allen BioData Registry PoC — structured JSON logging.

Every business Lambda logs in the same JSON shape so CloudWatch Logs
Insights queries are uniform across the registry. The shape is:

.. code-block:: json

    {
      "level": "INFO",
      "logger": "biodata_registry_shared.db",
      "message": "rolling back transaction",
      "timestamp": "2026-03-24T19:22:01.245Z",
      "request_id": "01J9ABCD",
      "lambda_function": "registration",
      "extra": { "...": "..." }
    }

Two helpers are provided:

* :func:`configure_logging` — sets up the root logger with a single
  JSON :class:`logging.StreamHandler`. Idempotent — safe to call from
  every handler entry point.
* :func:`bind_request_id` — context manager that injects the API
  Gateway request id into every log record emitted in the body. Use
  it as the outermost wrapper of every Lambda handler:

  .. code-block:: python

      def handler(event, context):
          configure_logging()
          with bind_request_id(_extract_request_id(event)):
              ...
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Iterator, Optional


# Context variable carrying the request id for the current execution.
# ContextVar is asyncio-safe and thread-safe; it survives across await
# points (irrelevant in synchronous Lambdas, but keeps the helper
# correct if we ever move to async handlers).
_REQUEST_ID_VAR: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "biodata_registry_request_id",
    default=None,
)


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


class _JSONFormatter(logging.Formatter):
    """Serialize a :class:`logging.LogRecord` as a single-line JSON object.

    Custom ``extra={...}`` dicts are merged under an ``extra`` key so
    we do not collide with the standard top-level fields. Any
    non-JSON-serializable objects in ``extra`` are coerced via
    ``default=str`` rather than crashing the log emit — losing
    fidelity in a single log line is preferable to losing the line
    entirely.
    """

    # Standard LogRecord attributes we want to skip when packing
    # ``extra``. Everything else on the record that isn't on this
    # list is treated as caller-supplied extra data.
    _RESERVED = frozenset(
        {
            "args",
            "asctime",
            "created",
            "exc_info",
            "exc_text",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "message",
            "module",
            "msecs",
            "msg",
            "name",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "thread",
            "threadName",
            "taskName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc)
        body: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "timestamp": ts.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "request_id": _REQUEST_ID_VAR.get() or "",
            "lambda_function": os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""),
        }

        extra = {
            k: v
            for k, v in record.__dict__.items()
            if k not in self._RESERVED and not k.startswith("_")
        }
        if extra:
            body["extra"] = extra

        if record.exc_info:
            body["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(body, default=str)


# ---------------------------------------------------------------------------
# Public configure
# ---------------------------------------------------------------------------


_CONFIGURED = False


def configure_logging(level: Optional[str] = None) -> None:
    """Set up the root logger with the JSON formatter.

    Idempotent — calling this from every Lambda handler entry point
    is safe and recommended (it ensures the formatter is set even on
    a cold start where the module hasn't been imported yet, and a
    no-op on warm starts).

    Parameters
    ----------
    level:
        Logging level name (e.g. ``"INFO"``). Falls back to the
        ``LOG_LEVEL`` env var, then ``INFO``.
    """
    global _CONFIGURED  # noqa: PLW0603 - intentional module-level cache
    root = logging.getLogger()

    desired_level_name = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    desired_level = logging.getLevelName(desired_level_name)
    if isinstance(desired_level, str):  # invalid name → INFO
        desired_level = logging.INFO

    root.setLevel(desired_level)

    if _CONFIGURED:
        # Already attached our formatter once; just refresh the level
        # in case the caller is overriding for a specific code path.
        return

    # Lambda's bootstrap pre-configures a default handler that emits
    # plain text. Replace it so our JSON formatter is the only thing
    # writing to stdout. Keeping multiple handlers would double-log.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_JSONFormatter())
    handler.setLevel(desired_level)
    root.addHandler(handler)

    _CONFIGURED = True


# ---------------------------------------------------------------------------
# Request id propagation
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def bind_request_id(request_id: Optional[str]) -> Iterator[Optional[str]]:
    """Bind ``request_id`` to every log record emitted in the body.

    Designed to wrap the handler body:

    .. code-block:: python

        def handler(event, context):
            rid = (
                event.get("requestContext", {}).get("requestId")
                or context.aws_request_id
            )
            with bind_request_id(rid):
                ...

    The bound id is propagated through :data:`_REQUEST_ID_VAR`, a
    :class:`contextvars.ContextVar`, so it survives across any nested
    function calls and does not leak between concurrent Lambda
    invocations (each invocation runs in its own context).

    Yields the bound id back to the caller so handlers can pass it
    to :func:`make_error_response` without re-extracting from the
    event.
    """
    token = _REQUEST_ID_VAR.set(request_id)
    try:
        yield request_id
    finally:
        _REQUEST_ID_VAR.reset(token)


def get_logger(name: str) -> logging.Logger:
    """Wrapper around :func:`logging.getLogger` for symmetry with the helpers above.

    Exists so callers can write ``from biodata_registry_shared import
    get_logger`` rather than mixing import styles.
    """
    return logging.getLogger(name)


__all__ = (
    "bind_request_id",
    "configure_logging",
    "get_logger",
)
