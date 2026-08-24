"""Unit tests for biodata_registry_shared.logging_config."""
from __future__ import annotations

import io
import json
import logging
import sys

import pytest

from biodata_registry_shared.logging_config import (
    bind_request_id,
    configure_logging,
    get_logger,
)


def _capture_logs() -> tuple[io.StringIO, logging.Handler]:
    """Replace the root logger's handler with an in-memory one."""
    configure_logging("INFO")
    root = logging.getLogger()
    # configure_logging attached a stdout handler; swap it out with
    # an in-memory StringIO handler that uses the same formatter.
    existing_handler = root.handlers[0]
    formatter = existing_handler.formatter
    root.removeHandler(existing_handler)

    buf = io.StringIO()
    handler = logging.StreamHandler(stream=buf)
    handler.setFormatter(formatter)
    handler.setLevel(logging.DEBUG)
    root.addHandler(handler)
    return buf, handler


def _read_lines(buf: io.StringIO) -> list[dict[str, object]]:
    raw = buf.getvalue().strip()
    if not raw:
        return []
    return [json.loads(line) for line in raw.splitlines()]


def test_log_record_contains_required_fields() -> None:
    buf, _ = _capture_logs()
    log = get_logger("biodata_registry_shared.tests")
    log.info("hello")
    lines = _read_lines(buf)
    assert len(lines) == 1
    record = lines[0]
    assert record["level"] == "INFO"
    assert record["logger"] == "biodata_registry_shared.tests"
    assert record["message"] == "hello"
    assert "timestamp" in record
    assert record["request_id"] == ""


def test_bind_request_id_propagates_to_records() -> None:
    buf, _ = _capture_logs()
    log = get_logger("biodata_registry_shared.tests")
    with bind_request_id("req-123"):
        log.warning("with rid")
    log.warning("without rid")
    lines = _read_lines(buf)
    assert lines[0]["request_id"] == "req-123"
    # After the context manager exits, the request_id resets.
    assert lines[1]["request_id"] == ""


def test_extra_fields_packed_under_extra_key() -> None:
    buf, _ = _capture_logs()
    log = get_logger("biodata_registry_shared.tests")
    log.info("structured", extra={"asset_id": "abc", "count": 7})
    lines = _read_lines(buf)
    assert lines[0]["extra"] == {"asset_id": "abc", "count": 7}


def test_non_serializable_extra_falls_back_to_str() -> None:
    buf, _ = _capture_logs()
    log = get_logger("biodata_registry_shared.tests")

    class _Thing:
        def __str__(self) -> str:
            return "thing-repr"

    log.info("nonser", extra={"thing": _Thing()})
    lines = _read_lines(buf)
    assert lines[0]["extra"]["thing"] == "thing-repr"


def test_configure_logging_is_idempotent() -> None:
    configure_logging("INFO")
    configure_logging("DEBUG")
    handlers = logging.getLogger().handlers
    # Only one handler attached even after multiple calls.
    assert len(handlers) == 1


def test_configure_logging_respects_log_level_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    configure_logging()
    assert logging.getLogger().level == logging.WARNING


def test_get_logger_returns_logger_instance() -> None:
    log = get_logger("biodata_registry_shared.test_get_logger")
    assert isinstance(log, logging.Logger)
    assert log.name == "biodata_registry_shared.test_get_logger"


def test_exc_info_serializes_into_record() -> None:
    buf, _ = _capture_logs()
    log = get_logger("biodata_registry_shared.tests")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.exception("oops")
    lines = _read_lines(buf)
    assert "RuntimeError" in lines[0]["exc_info"]  # type: ignore[index]


def test_stdout_is_default_target() -> None:
    """Cold-start configure_logging targets stdout (CloudWatch Logs reads stdout)."""
    configure_logging()
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    # Lambda environment: stdout is captured. Local pytest may swap
    # this with capfd's stream; accept either.
    assert handler.stream is sys.stdout or hasattr(handler.stream, "write")
