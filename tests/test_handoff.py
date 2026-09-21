import zipfile

from credit_extract.handoff import build_payload, emit
from tests.test_evaluate import doc as make_doc
from tests.test_review import fld


def test_payload_sends_only_trusted_values_unless_asked():
    d = make_doc(fields=[fld("f1", "Maturity Date", "October 28, 2027", status="auto_accepted", canonical="maturity_date"),
                         fld("f2", "Applicable Rate (Level 1)", "0.910%", canonical="applicable_margin"),
                         fld("f3", "Noise", "x", status="auto_accepted")])
    got = []
    emit(build_payload(d, 1), got.append)
    assert [v.key for v in got[0].values] == ["maturity_date"] and got[0].withheld_pending_review == 1
    assert got[0].values[0].quote and got[0].values[0].page == 2
    assert [v.status for v in build_payload(d, 1, include_pending=True).values] == ["auto_accepted", "needs_review"]


def test_export_audit_zip_has_manifest_and_no_secrets(tmp_path, scanned_pdf):
    from pathlib import Path

    from credit_extract.handoff import export_audit
    from credit_extract.ingest import ingest
    from credit_extract.store import Store

    store = Store(tmp_path / "store")
    run = ingest(store, scanned_pdf, "doc", "v1", dictionary_version=1)
    repo = Path(__file__).resolve().parents[1]
    m = export_audit(store, run.run_id, repo, tmp_path / "a.zip")
    names = zipfile.ZipFile(tmp_path / "a.zip").namelist()
    assert "MANIFEST.json" in names and "run/run.json" in names and "config/models.yaml" in names
    assert "dictionary/v0001.json" in names and not any(".env" in n for n in names)
    assert set(m["files"]) == set(names) - {"MANIFEST.json"}
