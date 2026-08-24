"""JSONB serialization / deserialization helpers for aind-data-schema models.

These helpers wrap the ``model_dump_json``-style pattern that
``handler.py`` already uses when writing Pydantic models into Aurora's
JSONB columns. The helpers are extracted out of the handler so that
the Property 5 round-trip test (Task 16.2) and any future
non-handler call site (CDC consumer, agent proposal applier, etc.)
share one canonical serialization path.

Round-trip contract (Property 5, R33.1 / R33.2 / R33.3)
-------------------------------------------------------

For any Pydantic model ``M`` and instance ``inst = M(...)``::

    deserialize_from_jsonb(serialize_to_jsonb(inst), M) == inst

This must hold over every value the schema admits — Decimal,
``datetime``/``date`` (tz-aware and naive), enums, nested models, etc.

Why ``mode='json'`` matters
---------------------------

``Pydantic.model_dump()`` returns a Python dict that may carry types
JSON does not natively model (``Decimal``, ``datetime``, ``date``,
``UUID``, ``Enum``). Persisting that dict directly via psycopg works
*on the way in* — psycopg's adapters know how to encode each — but
breaks on the way out because:

* JSONB stores the JSON string, not the Python object.
* ``Decimal`` round-trips to a JSON number; loading via ``json.loads``
  produces a ``float`` and Pydantic's strict-enough ``Decimal`` field
  compares unequal to the original (e.g. ``Decimal('1.10') != 1.1``).
* ``datetime`` is serialized as ISO-8601, but ``model_dump()`` stores
  the original ``datetime`` object; the deserialized value would be a
  string under non-JSON dump mode.
* ``Enum`` members serialize to ``Member`` (the object), not to their
  value, so the JSON encoder either fails or stores something the
  schema cannot validate back.

``mode='json'`` makes Pydantic apply the same JSON-compatible
coercion the writer side has to apply to the JSONB column anyway,
which is what gives the round trip its losslessness guarantee.

Decision: helpers operate on raw text (``str``) rather than dicts
-----------------------------------------------------------------

JSONB is text in the wire and text on the way back out (psycopg
materializes it as ``dict`` only because it sniffs the column type
post-fetch). We model the round trip as ``Model -> str -> Model`` so
the test exercises the ``json.dumps`` / ``json.loads`` boundary that
JSONB enforces in production. A dict-only contract would silently
hide ``Decimal`` / ``datetime`` issues.
"""

from __future__ import annotations

import json
from typing import Type, TypeVar

from pydantic import BaseModel


_M = TypeVar("_M", bound=BaseModel)


def serialize_to_jsonb(instance: BaseModel) -> str:
    """Serialize a Pydantic model into a JSONB-ready text string.

    Uses ``model_dump_json``, which internally applies the same
    JSON-compatible coercion as ``model_dump(mode='json')`` and also
    handles UTF-8 / control character escaping so the resulting text
    is safe to pass straight into a ``JSONB`` bind parameter.

    The output is the canonical Pydantic JSON representation — no
    custom encoders, no field re-ordering. This keeps the helper
    drop-in-replaceable for ``handler._to_jsonb`` when the value is
    a Pydantic model rather than a plain dict.
    """
    return instance.model_dump_json()


def deserialize_from_jsonb(text: str, model: Type[_M]) -> _M:
    """Deserialize a JSONB text string back into a Pydantic model.

    Uses ``model_validate_json`` directly on the text, which routes
    through Pydantic's parsing pipeline — Decimal fields recover from
    the JSON number with full precision because Pydantic parses
    numeric strings via the model's field type, not via Python's
    ``float`` (which would round 1.10 to 1.1).

    Parameters
    ----------
    text:
        JSONB text payload, as returned by :func:`serialize_to_jsonb`
        or as fetched from a Postgres JSONB column (``psycopg`` will
        give you a ``dict``; pass it through ``json.dumps`` first or
        use :func:`deserialize_from_jsonb_value`).
    model:
        The aind-data-schema Pydantic class to validate against.
    """
    return model.model_validate_json(text)


def deserialize_from_jsonb_value(value, model: Type[_M]) -> _M:
    """Deserialize a JSONB column value (dict or str) into a model.

    Convenience wrapper for the case where psycopg has already
    materialized the JSONB column into a Python dict. Re-serializes
    the dict back to text and parses through Pydantic so the same
    Decimal / datetime / enum handling applies.
    """
    if isinstance(value, str):
        return deserialize_from_jsonb(value, model)
    return deserialize_from_jsonb(json.dumps(value), model)
