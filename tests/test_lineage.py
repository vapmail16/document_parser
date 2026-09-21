from datetime import date

from credit_extract.lineage import as_of, load_index, publish
from credit_extract.store import Store
from tests.test_review import fld
from tests.test_evaluate import doc as make_doc


def versioned(label, eff, margin):
    d = make_doc(fields=[fld("f1", "Applicable Rate (Level 1)", margin, status="auto_accepted"
                             if False else "confirmed", canonical="applicable_margin")])
    return d.model_copy(update={"version_label": label, "effective_date": eff})


def test_publish_is_write_once_and_as_of_picks_the_version_in_force(tmp_path):
    store = Store(tmp_path)
    publish(store, versioned("AR-2010", date(2010, 7, 30), "2.50%"), "clean")
    publish(store, versioned("AR-2013", date(2013, 1, 18), "1.75%"), "clean")
    publish(store, versioned("AR-2013", date(2013, 1, 18), "1.80%"), "clean")  # re-review: new revision
    idx = load_index(store, "doc")
    assert [(r.version_label, r.revision) for r in idx.revisions] == [("AR-2010", 1), ("AR-2013", 1), ("AR-2013", 2)]
    assert as_of(store, "doc", "applicable_margin", on=date(2012, 6, 1)).fields[0].raw_value == "2.50%"
    a = as_of(store, "doc", "applicable_margin", on=date(2014, 1, 1))
    assert (a.version_label, a.revision, a.fields[0].raw_value) == ("AR-2013", 2, "1.80%")
    assert as_of(store, "doc", "applicable_margin", version_label="AR-2013").revision == 2
    assert as_of(store, "doc", "applicable_margin", on=date(2009, 1, 1)) is None
    assert len(list((tmp_path / "records" / "doc" / "AR-2013").glob("r*.json"))) == 2  # nothing overwritten
