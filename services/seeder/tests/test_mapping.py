"""Unit tests for ``mapping.py`` — the pure-functional record-to-row
mapper used by the seeder Lambda.

The tests cover the four behaviours required by Task 9.1:

1. **Mapping logic produces correct INSERTs for the 5-record fixture**
   — every row's column set matches the migration DDL, the natural
   keys are preserved, and the metadata blob carries the source
   record.
3. **Sampling fraction is deterministic** — same hash modulo gives
   the same subset; ``should_sample`` is total over `[0, 1]`; the
   sample size matches the requested fraction within statistical
   bounds for an N=10000 corpus.
4. **Missing optional fields don't crash** — degenerate records still
   produce a `MappedRecord` with a usable Data_Asset row.

Plus a property-based check that ``map_record`` never raises on
arbitrary dict-shaped inputs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import mapping


_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_records.json"

# Stable bootstrap ids the mapper does not actually validate — they
# just flow through into the row's ``space_id`` / ``created_by``
# columns.
_SPACE_ID = "00000000-0000-0000-0000-000000000001"
_USER_ID = "00000000-0000-0000-0000-000000000002"


# Columns the mapper must produce for each row, derived from the
# migrations 0001/0002/0003 DDL. The mapper is allowed to produce a
# subset (every column is nullable in the row dict — pg8000 will fill
# the column default), but everything it produces MUST appear in the
# table's column set or the INSERT would fail.

_DATA_ASSET_COLS = {
    "space_id", "name", "display_name", "storage_uri", "data_type",
    "lifecycle_state", "validation_status", "schema_version",
    "description", "metadata", "created_by",
}
_SUBJECT_COLS = {
    "subject_id", "species", "sex", "date_of_birth", "genotype",
    "source", "weight_at_acquisition_g", "age_at_acquisition_days",
    "notes", "metadata", "created_by",
}
_INSTRUMENT_COLS = {
    "instrument_id", "instrument_type", "manufacturer", "model",
    "serial_number", "calibration_date", "notes", "metadata",
    "created_by",
}
_RIG_COLS = {
    "rig_id", "modalities", "location", "notes", "metadata",
    "created_by",
}


# ---------------------------------------------------------------------------
# Fixture loading.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fixture_records() -> List[Dict[str, Any]]:
    """Load the canonical 5-record fixture once per module."""
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1) Mapping correctness over the 5-record fixture.
# ---------------------------------------------------------------------------


def test_mapping_produces_data_asset_for_every_record(
    fixture_records: List[Dict[str, Any]],
) -> None:
    for rec in fixture_records:
        m = mapping.map_record(rec, space_id=_SPACE_ID, created_by=_USER_ID)
        assert m is not None
        assert m.data_asset is not None
        assert m.data_asset["space_id"] == _SPACE_ID
        assert m.data_asset["created_by"] == _USER_ID
        assert set(m.data_asset.keys()).issubset(_DATA_ASSET_COLS)


def test_mapping_extracts_storage_uri_from_location_dict(
    fixture_records: List[Dict[str, Any]],
) -> None:
    """Most records carry the URI under ``location.s3_uri``."""
    rec = fixture_records[0]
    m = mapping.map_record(rec, space_id=_SPACE_ID, created_by=_USER_ID)
    assert m is not None and m.data_asset is not None
    assert (
        m.data_asset["storage_uri"]
        == "s3://aind-open-data/exaSPIM_695464_2024-09-12_18-03-29"
    )


def test_mapping_extracts_storage_uri_when_location_is_a_string(
    fixture_records: List[Dict[str, Any]],
) -> None:
    """The fMOST record carries ``location`` as a bare string."""
    fmost = next(
        r for r in fixture_records if r.get("modality") == "fMOST"
    )
    m = mapping.map_record(fmost, space_id=_SPACE_ID, created_by=_USER_ID)
    assert m is not None and m.data_asset is not None
    assert m.data_asset["storage_uri"].startswith("s3://aind-open-data/fmost_")


def test_mapping_falls_back_to_top_level_storage_uri(
    fixture_records: List[Dict[str, Any]],
) -> None:
    """Minimal record uses top-level ``storage_uri`` directly."""
    minimal = next(
        r for r in fixture_records if "minimal_record" in r.get("storage_uri", "")
    )
    m = mapping.map_record(minimal, space_id=_SPACE_ID, created_by=_USER_ID)
    assert m is not None and m.data_asset is not None
    assert (
        m.data_asset["storage_uri"]
        == "s3://aind-open-data/minimal_record_2024-11-01"
    )


def test_mapping_synthesises_storage_uri_when_missing() -> None:
    """A record with no plausible URI gets a deterministic seed:// uri."""
    rec = {"name": "no-uri-record", "modality": "behaviour"}
    m = mapping.map_record(rec, space_id=_SPACE_ID, created_by=_USER_ID)
    assert m is not None and m.data_asset is not None
    assert m.data_asset["storage_uri"].startswith("seed://no-storage-uri/")
    # Hash suffix is the content hash — deterministic.
    assert m.data_asset["storage_uri"].endswith(m.content_hash)


def test_mapping_emits_subject_for_records_with_subject(
    fixture_records: List[Dict[str, Any]],
) -> None:
    rec = fixture_records[0]
    m = mapping.map_record(rec, space_id=_SPACE_ID, created_by=_USER_ID)
    assert m is not None and m.subject is not None
    assert m.subject["subject_id"] == "695464"
    assert m.subject["species"] == "Mus musculus"
    assert m.subject["sex"] == "M"
    assert m.subject["date_of_birth"] == "2023-04-15"
    assert m.link_subject is True
    assert set(m.subject.keys()).issubset(_SUBJECT_COLS)


def test_mapping_emits_instrument_for_records_with_instrument(
    fixture_records: List[Dict[str, Any]],
) -> None:
    rec = fixture_records[0]
    m = mapping.map_record(rec, space_id=_SPACE_ID, created_by=_USER_ID)
    assert m is not None and m.instrument is not None
    assert m.instrument["instrument_id"] == "EXASPIM-001"
    assert m.instrument["instrument_type"] == "ExA-SPIM"
    assert m.link_instrument is True
    assert set(m.instrument.keys()).issubset(_INSTRUMENT_COLS)


def test_mapping_emits_rig_with_modalities_list(
    fixture_records: List[Dict[str, Any]],
) -> None:
    rec = fixture_records[1]  # ophys, modalities = ["ophys", "behaviour"]
    m = mapping.map_record(rec, space_id=_SPACE_ID, created_by=_USER_ID)
    assert m is not None and m.rig is not None
    assert m.rig["rig_id"] == "mesoscope-rig-A"
    assert m.rig["modalities"] == ["ophys", "behaviour"]
    assert set(m.rig.keys()).issubset(_RIG_COLS)


def test_mapping_emits_session_when_present(
    fixture_records: List[Dict[str, Any]],
) -> None:
    rec = fixture_records[0]
    m = mapping.map_record(rec, space_id=_SPACE_ID, created_by=_USER_ID)
    assert m is not None and m.session is not None
    assert m.session["session_id"] == "exaSPIM-695464-S01"
    assert m.session["session_type"] == "imaging"
    assert m.session["session_start"] == "2024-09-12T18:03:29Z"


def test_mapping_emits_acquisition_processing_qc_data_description(
    fixture_records: List[Dict[str, Any]],
) -> None:
    rec = fixture_records[0]
    m = mapping.map_record(rec, space_id=_SPACE_ID, created_by=_USER_ID)
    assert m is not None
    assert m.acquisition is not None
    assert m.acquisition["acquisition_start"] == "2024-09-12T18:30:00Z"
    assert m.processing is not None
    assert m.processing["processing_pipeline"] == "aind-spim-pipeline"
    assert m.processing["version"] == "1.4.0"
    assert m.quality_control is not None
    assert m.quality_control["qc_metric"] == "stitch_score"
    assert m.quality_control["value"] == 0.94
    assert m.data_description is not None
    assert m.data_description["text"].startswith("Whole-brain ExA-SPIM")
    assert m.data_description["license"] == "CC-BY-4.0"


def test_mapping_preserves_full_source_record_in_metadata(
    fixture_records: List[Dict[str, Any]],
) -> None:
    """The ``metadata`` blob on data_asset must carry the source record."""
    rec = fixture_records[0]
    m = mapping.map_record(rec, space_id=_SPACE_ID, created_by=_USER_ID)
    assert m is not None and m.data_asset is not None
    md = m.data_asset["metadata"]
    assert "source_record" in md
    # Round-trips through json without loss for the keys we care about.
    serialized = json.dumps(md["source_record"], sort_keys=True)
    assert "exaSPIM_subject_695464" in serialized
    assert md["__seeder"]["content_hash"] == m.content_hash


def test_mapping_returns_none_for_empty_or_non_dict_input() -> None:
    assert mapping.map_record({}, space_id=_SPACE_ID, created_by=_USER_ID) is None
    # The function signature is `Mapping`, but None should also be safe.
    assert mapping.map_record(None, space_id=_SPACE_ID, created_by=_USER_ID) is None  # type: ignore[arg-type]
    assert mapping.map_record([], space_id=_SPACE_ID, created_by=_USER_ID) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Missing optional fields don't crash.
# ---------------------------------------------------------------------------


def test_mapping_handles_minimal_record(
    fixture_records: List[Dict[str, Any]],
) -> None:
    minimal = next(
        r for r in fixture_records if "minimal_record" in r.get("storage_uri", "")
    )
    m = mapping.map_record(minimal, space_id=_SPACE_ID, created_by=_USER_ID)
    assert m is not None
    assert m.data_asset is not None
    # No subject/instrument/etc. attached.
    assert m.subject is None
    assert m.instrument is None
    assert m.rig is None
    assert m.procedures is None
    assert m.session is None
    assert m.link_subject is False
    assert m.link_instrument is False
    assert m.link_rig is False
    assert m.link_procedures is False


def test_mapping_handles_subject_without_optional_fields() -> None:
    rec = {
        "storage_uri": "s3://test/asset/1",
        "subject": {"subject_id": "s1", "species": "Mus musculus"},
    }
    m = mapping.map_record(rec, space_id=_SPACE_ID, created_by=_USER_ID)
    assert m is not None and m.subject is not None
    assert m.subject["subject_id"] == "s1"
    assert m.subject["species"] == "Mus musculus"
    # Optional fields are present but None.
    assert m.subject["sex"] is None
    assert m.subject["date_of_birth"] is None
    assert m.subject["genotype"] is None


def test_mapping_synthesises_subject_id_when_missing() -> None:
    rec = {
        "storage_uri": "s3://test/asset/synth-subj",
        "subject": {"species": "Mus musculus"},
    }
    m = mapping.map_record(rec, space_id=_SPACE_ID, created_by=_USER_ID)
    assert m is not None and m.subject is not None
    assert m.subject["subject_id"].startswith("seed-subject-")


def test_mapping_skips_empty_optional_objects() -> None:
    """An empty ``{}`` for ``subject`` is treated as absent."""
    rec = {"storage_uri": "s3://test/asset/empty-subj", "subject": {}}
    m = mapping.map_record(rec, space_id=_SPACE_ID, created_by=_USER_ID)
    assert m is not None
    assert m.subject is None
    assert m.link_subject is False


def test_mapping_drops_invalid_dates_silently() -> None:
    rec = {
        "storage_uri": "s3://test/asset/bad-dob",
        "subject": {
            "subject_id": "s2",
            "species": "Mus musculus",
            "date_of_birth": "not-a-date",
        },
    }
    m = mapping.map_record(rec, space_id=_SPACE_ID, created_by=_USER_ID)
    assert m is not None and m.subject is not None
    assert m.subject["date_of_birth"] is None


def test_mapping_drops_invalid_numerics_silently() -> None:
    rec = {
        "storage_uri": "s3://test/asset/bad-weight",
        "subject": {
            "subject_id": "s3",
            "species": "Mus musculus",
            "weight_at_acquisition_g": "not-a-number",
        },
    }
    m = mapping.map_record(rec, space_id=_SPACE_ID, created_by=_USER_ID)
    assert m is not None and m.subject is not None
    assert m.subject["weight_at_acquisition_g"] is None


def test_mapping_handles_modalities_as_string() -> None:
    rec = {
        "storage_uri": "s3://test/asset/string-modality",
        "rig": {"rig_id": "r1", "modalities": "ophys"},
    }
    m = mapping.map_record(rec, space_id=_SPACE_ID, created_by=_USER_ID)
    assert m is not None and m.rig is not None
    assert m.rig["modalities"] == ["ophys"]


# ---------------------------------------------------------------------------
# 3) Sampling determinism.
# ---------------------------------------------------------------------------


def test_should_sample_is_deterministic_per_hash() -> None:
    """Same hash + same fraction → same decision, every time."""
    h = mapping.compute_content_hash({"key": "value"})
    decisions = [mapping.should_sample(h, 0.1) for _ in range(100)]
    assert all(d == decisions[0] for d in decisions)


def test_should_sample_zero_rejects_all() -> None:
    for n in range(50):
        h = mapping.compute_content_hash({"i": n})
        assert mapping.should_sample(h, 0.0) is False


def test_should_sample_one_accepts_all() -> None:
    for n in range(50):
        h = mapping.compute_content_hash({"i": n})
        assert mapping.should_sample(h, 1.0) is True


def test_should_sample_negative_rejects_all() -> None:
    h = mapping.compute_content_hash({"k": "v"})
    assert mapping.should_sample(h, -0.5) is False


def test_should_sample_above_one_accepts_all() -> None:
    h = mapping.compute_content_hash({"k": "v"})
    assert mapping.should_sample(h, 5.0) is True


def test_should_sample_distribution_matches_fraction_within_tolerance() -> None:
    """Over a large corpus, the sampled fraction matches the requested
    fraction within statistical noise."""
    n = 10_000
    fraction = 0.1
    accepted = sum(
        1
        for i in range(n)
        if mapping.should_sample(mapping.compute_content_hash({"i": i}), fraction)
    )
    # 99.9% confidence interval for a binomial(n=10_000, p=0.1) is
    # roughly [0.0883, 0.1117]. We use a wider window for portability.
    assert 0.07 * n <= accepted <= 0.13 * n, (
        f"sampled {accepted}/{n} ({accepted/n:.3f}); expected near {fraction}"
    )


def test_should_sample_subset_is_stable_across_runs() -> None:
    """Re-running the seeder picks the same record subset."""
    n = 1000
    fraction = 0.2
    hashes = [mapping.compute_content_hash({"i": i}) for i in range(n)]
    pass1 = {h for h in hashes if mapping.should_sample(h, fraction)}
    pass2 = {h for h in hashes if mapping.should_sample(h, fraction)}
    assert pass1 == pass2


def test_compute_content_hash_is_canonical() -> None:
    """Key order does not affect the hash."""
    a = {"a": 1, "b": [2, 3], "c": {"x": "y"}}
    b = {"c": {"x": "y"}, "b": [2, 3], "a": 1}
    assert mapping.compute_content_hash(a) == mapping.compute_content_hash(b)


def test_compute_content_hash_handles_non_json_values() -> None:
    """Non-JSON values fall through ``default=str``."""
    import datetime

    rec = {"when": datetime.date(2024, 1, 2)}
    h = mapping.compute_content_hash(rec)
    assert isinstance(h, str) and len(h) == 64


# ---------------------------------------------------------------------------
# Property-based: map_record never raises.
# ---------------------------------------------------------------------------


# Generate plausible record-shaped dicts: mix top-level scalar keys
# with optional nested entity dicts.
_scalars = st.one_of(
    st.none(), st.booleans(), st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
)
_optional_dict = st.one_of(
    st.none(),
    st.dictionaries(
        keys=st.text(min_size=1, max_size=12),
        values=_scalars,
        max_size=5,
    ),
)


@st.composite
def _records(draw: Any) -> Dict[str, Any]:
    base = draw(
        st.dictionaries(
            keys=st.text(min_size=1, max_size=12),
            values=_scalars,
            max_size=8,
        )
    )
    for key in (
        "subject", "instrument", "rig", "procedures",
        "session", "acquisition", "processing",
        "quality_control", "data_description",
    ):
        d = draw(_optional_dict)
        if d is not None:
            base[key] = d
    # Force a non-empty dict so map_record returns a value.
    if not base:
        base["_seed"] = "non-empty"
    return base


@given(record=_records())
@settings(
    max_examples=80,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
def test_property_map_record_never_raises_on_dict_input(
    record: Dict[str, Any]
) -> None:
    """For arbitrary dict-shaped records, map_record returns a
    MappedRecord (or None for an empty dict) and never raises.

    Validates: R32.5 (re-run safety — degenerate inputs must not break
    the seed loop).
    """
    result = mapping.map_record(
        record, space_id=_SPACE_ID, created_by=_USER_ID
    )
    if result is None:
        assert len(record) == 0
        return
    assert result.content_hash == mapping.compute_content_hash(record)
    # Data_Asset row is always present when a result is returned.
    assert result.data_asset is not None
    assert result.data_asset["storage_uri"]
