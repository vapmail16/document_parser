from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from credit_extract.contracts import DictionarySnapshot, FieldDictionaryEntry, normalise_surface_form
from credit_extract.dictionary import list_versions, load_snapshot, save_snapshot

REPO = Path(__file__).resolve().parents[1]
SNAPSHOTS = REPO / "dictionary" / "snapshots"


def entry(name: str, display: str, aliases=()) -> FieldDictionaryEntry:
    return FieldDictionaryEntry(
        canonical_name=name, display_name=display,
        aliases=[{"surface_form": a, "status": "approved", "learned_via": "seed"} for a in aliases],
        first_seen="2026-09-21", last_seen="2026-09-21",
    )


def snapshot(version=1, parent=None, entries=(), pairs=()) -> DictionarySnapshot:
    return DictionarySnapshot(version=version, parent_version=parent, created_at=datetime.now(timezone.utc),
                              entries=list(entries), confusable_pairs=list(pairs))


def test_seed_snapshot_loads_and_has_core_set():
    snap = load_snapshot(SNAPSHOTS, 1)
    assert snap.version == 1
    assert 15 <= len(snap.core_fields) <= 30
    assert all(e.status == "approved" for e in snap.entries)


def test_seed_confusable_pairs_are_split_across_entries():
    # Guard that the two leverage ratios, and the two EBITDA terms, stay separate concepts.
    snap = load_snapshot(SNAPSHOTS)
    assert snap.entry("max_total_leverage_ratio") != snap.entry("max_senior_secured_leverage_ratio")
    assert ("Consolidated EBITDA", "Consolidated Adjusted EBITDA") in snap.confusable_pairs


def test_normalise_surface_form():
    assert normalise_surface_form("  Applicable-Margin ") == normalise_surface_form("applicable margin")


def test_surface_form_cannot_map_to_two_entries():
    with pytest.raises(ValidationError, match="maps to both"):
        snapshot(entries=[entry("a", "Margin"), entry("b", "Spread", aliases=["margin"])])


def test_confusable_pair_cannot_share_an_entry():
    with pytest.raises(ValidationError, match="both surface forms"):
        snapshot(entries=[entry("ebitda", "Consolidated EBITDA", aliases=["Consolidated Adjusted EBITDA"])],
                 pairs=[("Consolidated EBITDA", "Consolidated Adjusted EBITDA")])


def test_snapshots_are_immutable_and_sequential(tmp_path):
    save_snapshot(tmp_path, snapshot(1))
    with pytest.raises(ValueError, match="next snapshot must be v2"):
        save_snapshot(tmp_path, snapshot(1))
    with pytest.raises(ValueError, match="parent_version=1"):
        save_snapshot(tmp_path, snapshot(2))
    save_snapshot(tmp_path, snapshot(2, parent=1))
    assert list_versions(tmp_path) == [1, 2]
    assert load_snapshot(tmp_path).version == 2
    assert load_snapshot(tmp_path, 1).version == 1


def test_v2_only_adds_instance_rules_to_v1():
    v1, v2 = load_snapshot(SNAPSHOTS, 1), load_snapshot(SNAPSHOTS, 2)
    assert v2.parent_version == 1
    strip = lambda e: e.model_dump(exclude={"instance_rules", "version"})
    assert [strip(e) for e in v1.entries] == [strip(e) for e in v2.entries]
    assert v2.entry("applicable_margin").instance_rules
