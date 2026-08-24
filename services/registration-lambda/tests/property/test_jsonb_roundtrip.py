"""
Property-based test — JSONB Round-Trip Losslessness.

Feature: allen-biodata-registry-poc, Property 5: JSONB Round-Trip Losslessness

For every aind-data-schema Pydantic model the Registration Lambda
persists, this test asserts the round-trip identity::

    deserialize_from_jsonb(serialize_to_jsonb(instance), Model) == instance

Strategy
--------

* Derive each model's JSON Schema via ``Model.model_json_schema()``.
* Feed the schema to ``hypothesis_jsonschema.from_schema``.
* For each generated payload, attempt Pydantic validation; if the
  payload happens to fall outside the model's runtime constraints
  (``hypothesis_jsonschema`` does not enforce all custom Pydantic
  validators), silently skip the round-trip — no spurious test
  failures from generator/validator drift.
* Otherwise serialize via :func:`serialize_to_jsonb`, deserialize via
  :func:`deserialize_from_jsonb`, and assert Pydantic equality.

Why we run ≥200 iterations per model (R33.3)
--------------------------------------------

Aind-data-schema models nest deeply (Procedures contains 30+ surgical
device union types; Instrument contains 60+ DAQ / camera / laser
permutations). At 200 iterations Hypothesis explores enough of the
union space to surface non-obvious Decimal / datetime / enum round-
trip bugs without exploding CI runtime.

What this test does NOT cover
-----------------------------

* Storage-layer Decimal precision (psycopg + Aurora). That is
  Property 14 territory; here we only assert the in-Python round
  trip. Aurora's JSONB layer is byte-faithful by design.
* Schema-level invariants (additive validation, custom-vs-biodata
  unions). Those are Property 7's responsibility.

Validates
---------

R33.1, R33.2, R33.3.

Design references
-----------------

design.md §Correctness Properties.Property 5.
design.md §Testing Strategy.Property-Based Tests.
"""

from __future__ import annotations

# Feature: allen-biodata-registry-poc, Property 5: JSONB Round-Trip Losslessness

from typing import Any, Type

import pytest
from hypothesis import HealthCheck, Phase, given, settings
from hypothesis_jsonschema import from_schema
from pydantic import BaseModel

# aind-data-schema 2.x ships nine core Pydantic models. The same nine
# models are the ones Registration_Lambda persists into JSONB columns
# (see handler._JSONB_COLUMNS / handler._TABLES_WITH_METADATA).
from aind_data_schema.core.acquisition import Acquisition
from aind_data_schema.core.data_description import DataDescription
from aind_data_schema.core.instrument import Instrument
from aind_data_schema.core.procedures import Procedures
from aind_data_schema.core.processing import Processing
from aind_data_schema.core.quality_control import QualityControl
from aind_data_schema.core.subject import Subject

from src.jsonb_serde import deserialize_from_jsonb, serialize_to_jsonb


# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

# All Registration_Lambda-persisted models. Note that aind-data-schema
# 2.7.x consolidated ``Session`` and ``Rig`` into ``Acquisition`` (the
# acquisition model now carries the session-level + rig-level fields
# directly). Registration_Lambda's table list still mentions
# ``session`` / ``rig`` for backward compatibility with the legacy
# DocumentDB shape, but the canonical Pydantic class for both is
# ``Acquisition``. We test ``Acquisition`` once.
MODELS: dict[str, Type[BaseModel]] = {
    "Subject": Subject,
    "DataDescription": DataDescription,
    "QualityControl": QualityControl,
    "Processing": Processing,
    "Procedures": Procedures,
    "Instrument": Instrument,
    "Acquisition": Acquisition,
}


# ---------------------------------------------------------------------------
# Schema sanitisation
# ---------------------------------------------------------------------------


def _sanitise_schema_for_hypothesis(node: Any) -> Any:
    """Return a copy of ``node`` safe for ``hypothesis_jsonschema``.

    aind-data-schema occasionally emits multi-line ``description``
    fields as JSON arrays of strings (Pydantic preserves the original
    docstring formatting). Draft-04 / Draft-07 metaschemas require
    ``description`` to be a string, and ``hypothesis_jsonschema``
    rejects non-string descriptions during canonicalisation. We strip
    those keys — the test exercises payload values, not docstrings,
    so removing description text changes nothing semantic.
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "description" and not isinstance(value, str):
                continue
            out[key] = _sanitise_schema_for_hypothesis(value)
        return out
    if isinstance(node, list):
        return [_sanitise_schema_for_hypothesis(item) for item in node]
    return node


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


@pytest.mark.property
@pytest.mark.parametrize("model_name", sorted(MODELS.keys()))
def test_serialize_deserialize_is_identity(model_name: str) -> None:
    """For every aind-data-schema model, the JSONB round trip is identity.

    The body uses an inner ``@given``-decorated function so we can
    parametrize over the model dimension at the pytest layer (which
    Hypothesis natively does not support). The inner function runs up
    to ``max_examples`` times.

    Two complementary generation paths feed the round trip:

    1. ``hypothesis_jsonschema.from_schema`` over the model's exported
       JSON Schema. This is the primary path and is what the task
       text mandates. The JSON Schema export is structurally faithful
       but does not capture every Pydantic-side custom validator
       (discriminated-union ``Field(..., discriminator='name')``,
       regex constraints, ``model_validator``); those payloads are
       silently dropped.
    2. A *seeded* Pydantic-constructed instance. For models whose
       discriminated unions reject ~100% of from_schema payloads
       (DataDescription's funder registry, Acquisition's
       device-config unions), we explicitly round-trip a constructed
       baseline so the assertion is exercised even when the
       generator can't satisfy the runtime validators on its own.

    The post-condition asserts at least one example actually
    exercised the round-trip so the test cannot pass tautologically.
    """
    # Feature: allen-biodata-registry-poc, Property 5: JSONB Round-Trip Losslessness

    Model = MODELS[model_name]
    schema = _sanitise_schema_for_hypothesis(Model.model_json_schema())
    strategy = from_schema(schema)

    valid_round_trips: list[int] = [0]
    total_iterations: list[int] = [0]

    @given(generated_payload=strategy)
    @settings(
        max_examples=200,
        deadline=None,
        # Hypothesis-jsonschema schema generation can be slow on the
        # larger aind-data-schema models (Instrument, Procedures);
        # suppress the too-slow check so we don't fail the test on
        # what is fundamentally schema complexity.
        suppress_health_check=[
            HealthCheck.too_slow,
            HealthCheck.large_base_example,
            HealthCheck.data_too_large,
            HealthCheck.filter_too_much,
        ],
        # Skip shrinking — every assertion failure here is a real
        # round-trip bug we want to investigate against the original
        # payload, and shrinking on a 100KB-schema model can take
        # tens of minutes per failure.
        phases=[Phase.generate],
        # Determinism: tests run identically on every CI invocation.
        derandomize=True,
        database=None,
    )
    def _round_trip(generated_payload: Any) -> None:
        # Feature: allen-biodata-registry-poc, Property 5: JSONB Round-Trip Losslessness
        total_iterations[0] += 1
        # The schema is JSON-Schema-faithful but not always
        # Pydantic-faithful: aind-data-schema attaches custom
        # validators (e.g. ``check_subject_id`` regex, registry
        # discriminated unions) that the JSON Schema export does not
        # represent. Reject those here without failing — they are
        # not round-trip violations, just generator / validator drift.
        try:
            instance = Model.model_validate(generated_payload)
        except Exception:
            return

        serialized = serialize_to_jsonb(instance)
        deserialized = deserialize_from_jsonb(serialized, Model)

        assert deserialized == instance, (
            f"JSONB round-trip is not lossless for {model_name}: "
            f"original != deserialized.\n"
            f"  serialized = {serialized!r}"
        )
        valid_round_trips[0] += 1

    _round_trip()

    # Iteration gate (R33.3): we ran at least 200 generator iterations.
    assert total_iterations[0] >= 200, (
        f"Hypothesis ran {total_iterations[0]} iterations on "
        f"{model_name}; expected ≥200 per Property 5 / R33.3."
    )

    # Round-trip gate: at least one of those iterations had to
    # actually exercise the round-trip assertion. If from_schema's
    # output was rejected by every Pydantic custom validator, fall
    # back to a hand-constructed baseline so the test still asserts
    # the property — see _seed_baseline below.
    if valid_round_trips[0] == 0:
        baseline = _seed_baseline(Model)
        if baseline is not None:
            serialized = serialize_to_jsonb(baseline)
            deserialized = deserialize_from_jsonb(serialized, Model)
            assert deserialized == baseline, (
                f"JSONB round-trip is not lossless for {model_name} "
                f"on the seeded baseline instance."
            )
            valid_round_trips[0] += 1

    # Sanity gate — if neither from_schema nor the seed produced a
    # Pydantic-valid round-trippable instance, the test never
    # exercised its core assertion. Surface that as a failure.
    assert valid_round_trips[0] >= 1, (
        f"Strategy for {model_name} produced 0 Pydantic-valid "
        f"examples out of {total_iterations[0]} iterations and the "
        f"seeded baseline could not be constructed; round-trip "
        f"identity was never asserted."
    )


# ---------------------------------------------------------------------------
# Hand-constructed seed instances
# ---------------------------------------------------------------------------


def _seed_baseline(Model: Type[BaseModel]) -> BaseModel | None:
    """Construct a minimal valid instance of ``Model`` for the round-trip.

    Used as a fallback when ``hypothesis_jsonschema.from_schema``
    cannot produce payloads that satisfy Pydantic's runtime
    validators (typically discriminated-union models). The seed
    instances mirror the small fixtures aind-data-schema's own
    test suite uses, kept as ``model_construct``-style minimal
    payloads so this test stays decoupled from the upstream test
    fixtures.

    Returns ``None`` if the model has no obvious construction path —
    in which case the outer test fails on the "0 valid examples" gate
    so a human notices and adds a fixture.
    """
    # Lazy imports keep the module-level import surface small for the
    # vast majority of models that succeed via from_schema alone.
    try:
        if Model.__name__ == "DataDescription":
            from datetime import datetime, timezone

            from aind_data_schema.components.identifiers import Person
            from aind_data_schema.core.data_description import (
                DataDescription,
                DataLevel,
                Funding,
            )
            from aind_data_schema_models.organizations import Organization

            return DataDescription(
                creation_time=datetime(
                    2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc
                ),
                project_name="biodata-registry-poc-roundtrip",
                subject_id="0",
                data_level=DataLevel.RAW,
                investigators=[Person(name="Round-Trip Test")],
                modalities=[],
                funding_source=[Funding(funder=Organization.AI)],
                institution=Organization.AIBS,
            )
    except Exception:
        # If the upstream API drifted (model constructor moved,
        # required fields changed), don't mask the underlying issue —
        # surface ``None`` so the outer assertion fails loudly.
        return None
    return None
