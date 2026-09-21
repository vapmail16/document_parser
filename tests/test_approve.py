from pathlib import Path

import pytest

from credit_extract.approve import alias_form, approve
from credit_extract.dictionary import load_snapshot
from credit_extract.reconcile import map_field
from credit_extract.review import AliasProposal
from tests.test_reconcile import fld

SNAP = load_snapshot(Path(__file__).resolve().parents[1] / "dictionary" / "snapshots", 3)


def prop(name, key="applicable_margin"):
    return AliasProposal(canonical_name=key, surface_form=name, approved_by="Jane Smith", source="b1")


def test_alias_form_strips_qualifiers():
    assert alias_form("Applicable Rate for Base Rate Loans (Level 3)") == "Applicable Rate for Base Rate Loans"


def test_approved_alias_turns_fuzzy_into_alias_match_next_time():
    f = fld("Applicable Rate for Base Rate Loans (Level 3)", "0.125%")
    assert map_field(f, None, SNAP).method == "fuzzy"
    new, r = approve(SNAP, [prop("Applicable Rate for Base Rate Loans (Level 1)")], "Jane Smith")
    assert new.version == SNAP.version + 1 and r.added
    m = map_field(f, None, new)
    assert (m.method, m.instance_key) == ("alias", "level3|base_rate")
    assert SNAP.entry("applicable_margin").aliases != new.entry("applicable_margin").aliases  # old snapshot untouched


def test_ai_cannot_approve():
    with pytest.raises(PermissionError):
        approve(SNAP, [prop("Applicable Rate for Base Rate Loans")], "Claude")


def test_alias_breaking_confusable_rules_is_rejected():
    _, r = approve(SNAP, [prop("Consolidated Adjusted EBITDA", "consolidated_ebitda_definition")], "Jane Smith")
    assert not r.added and r.rejected


def test_new_entries_need_a_human_and_land_as_approved():
    from datetime import date

    from credit_extract.approve import add_entries
    from credit_extract.contracts import FieldDictionaryEntry

    e = FieldDictionaryEntry(canonical_name="pro_rata_share", display_name="Pro Rata Share",
                             business_meaning="Each lender's share of the facility.", first_seen=date(2026, 9, 21),
                             last_seen=date(2026, 9, 21))
    with pytest.raises(PermissionError):
        add_entries(SNAP, [e], "Claude")
    new = add_entries(SNAP, [e], "Jane Smith")
    assert new.entry("pro_rata_share").status == "approved" and new.version == SNAP.version + 1
