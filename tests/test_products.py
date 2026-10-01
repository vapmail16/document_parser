"""Entity model, data products, query register, REST, and MCP."""

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest

from credit_extract.contracts import Provenance
from credit_extract.handoff import HandoffPayload, HandoffValue
from credit_extract.products.access import AccessLayer, QueryError, open_access
from credit_extract.products.mcp_server import handle_rpc, read_message, write_message
from credit_extract.products.model import build_entity_model, build_products, write_bundle
from credit_extract.products.query_layer import time_aggregate, top_n
from credit_extract.products.register import QuerySpec, load_register
from credit_extract.products.rest import dispatch
from credit_extract.contracts import NormalisedValue

HANDOFF = Path(__file__).resolve().parents[1] / "var" / "handoff" / "albemarle-2022-AR.json"


def _provenance() -> Provenance:
    return Provenance(
        source_sha256="a" * 64, page_count=2, pipeline_version="0.1.0", model_id="test",
        model_params={}, prompt_versions={"pass1_extraction": "v1"}, dictionary_version=1,
        ocr_engine="test", run_id="run-1", extracted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _value(key, raw, value, page=1, instance=None, status="auto_accepted"):
    return HandoffValue(
        key=key, instance=instance, value=value, raw_value=raw, page=page, quote=raw,
        grounding="verified", status=status, origin="model",
    )


def _payload(values, **overrides) -> HandoffPayload:
    data = dict(
        document_id="doc", version_label="v1", revision=1, effective_date=date(2022, 10, 28),
        execution_status="executed", document_kind="restatement", parties=[], values=values,
        withheld_pending_review=0, provenance=_provenance(),
        emitted_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    data.update(overrides)
    return HandoffPayload(**data)


def _money(amount):
    return NormalisedValue(type="money", amount=Decimal(amount))


def _text(text):
    return NormalisedValue(type="text", text=text)


def _date(iso):
    return NormalisedValue(type="date", date_value=date.fromisoformat(iso))


def test_queries_run_from_the_http_worker_thread(tmp_path):
    """ThreadingHTTPServer calls the access layer off the thread that opened SQLite."""
    import threading

    payload = _payload([
        _value("lender", "Alpha", _text("Alpha"), instance="alpha"),
        _value("lender_commitment", "300", _money("300"), instance="alpha"),
    ])
    write_bundle(payload, tmp_path)
    access, _ = open_access(tmp_path)
    found = []

    def run():
        found.append(access.run("top_lenders", {"document_id": "doc", "version_label": "v1", "n": 1}))

    worker = threading.Thread(target=run)
    worker.start()
    worker.join()
    assert found[0]["rows"][0]["lender_name"] == "Alpha"


def test_top_n_orders_numbers_and_keeps_missing_last():
    rows = [{"name": "a", "amount": "20"}, {"name": "b", "amount": None}, {"name": "c", "amount": "100"}]
    assert [r["name"] for r in top_n(rows, 2, "amount")] == ["c", "a"]
    with pytest.raises(ValueError):
        top_n(rows, 0, "amount")


def test_time_aggregate_buckets_and_reports_skipped_dates():
    rows = [
        {"effective_date": "2022-10-28", "commitment_amount": "100"},
        {"effective_date": "2023-02-01", "commitment_amount": "40"},
        {"effective_date": "2023-11-01", "commitment_amount": "10"},
        {"effective_date": None, "commitment_amount": "5"},
    ]
    buckets, skipped = time_aggregate(rows, "effective_date", "year", "commitment_amount")
    assert skipped == 1
    assert buckets == [
        {"period": "2022", "count": 1, "sum": "100"},
        {"period": "2023", "count": 2, "sum": "50"},
    ]
    quarters, _ = time_aggregate(rows[:1], "effective_date", "quarter", "commitment_amount")
    assert quarters[0]["period"] == "2022-Q4"


def test_products_collapse_equal_values_and_keep_disagreements():
    payload = _payload([
        _value("total_commitments", "$1", _money("1500000000"), page=15),
        _value("total_commitments", "$1.00", _money("1500000000.00"), page=154),
        _value("total_commitments", "$2", _money("10"), page=3),
        _value("borrower", "ALBEMARLE CORPORATION", _text("ALBEMARLE CORPORATION"), instance="albemarle_corporation"),
        _value("borrower", "ALBEMARLE CORPORATION", _text("Albemarle Corporation"), page=2, instance="albemarle_corporation"),
        _value("lender", "Bank of America, N.A.", _text("Bank of America, N.A."), instance="bank_of_america_n_a", page=154),
        _value("lender_commitment", "$170", _money("170000000.00"), instance="bank_of_america_n_a", page=154),
        _value("administrative_agent", "BANK OF AMERICA, N.A.", _text("BANK OF AMERICA, N.A."), page=1),
    ])
    products = build_products(payload)
    terms = products["facility_terms"]["rows"]
    assert len(terms) == 2 and all(row["conflict"] == 1 for row in terms)
    amounts = {Decimal(row["amount"]) for row in terms}
    assert amounts == {Decimal("1500000000"), Decimal("10")}
    borrowers = [r for r in products["party_roles"]["rows"] if r["role"] == "borrower"]
    assert len(borrowers) == 1 and borrowers[0]["party_id"] == "party:doc:albemarle_corporation"
    agent = next(r for r in products["party_roles"]["rows"] if r["role"] == "administrative_agent")
    lender = next(r for r in products["party_roles"]["rows"] if r["role"] == "lender")
    assert agent["party_id"] == lender["party_id"] == "party:doc:bank_of_america_n_a"
    commit = products["lender_commitments"]["rows"][0]
    assert commit["lender_name"] == "Bank of America, N.A."
    assert Decimal(commit["commitment_amount"]) == Decimal("170000000")
    model = build_entity_model(payload, products)
    types = {e["type"] for e in model["entities"]}
    assert types == {"Facility", "Party", "Commitment", "Term"}
    assert any(r["type"] == "commits" for r in model["relationships"])
    assert model["source"]["accepted_values"] == len(payload.values)
    assert len(products["field_facts"]["rows"]) == len(payload.values)


def test_register_rejects_sql_that_is_not_a_single_read():
    with pytest.raises(ValueError):
        QuerySpec(name="bad", description="no", product="lender_commitments", sql="DELETE FROM lender_commitments")
    with pytest.raises(ValueError):
        QuerySpec(
            name="joined", description="no", product="lender_commitments",
            sql="SELECT * FROM lender_commitments JOIN facility_terms ON 1=1",
        )


def test_access_runs_only_registered_queries(tmp_path):
    payload = _payload([
        _value("lender", "Alpha", _text("Alpha"), instance="alpha"),
        _value("lender_commitment", "300", _money("300"), instance="alpha"),
        _value("lender", "Beta", _text("Beta"), instance="beta"),
        _value("lender_commitment", "100", _money("100"), instance="beta"),
        _value("maturity_date", "2027-10-28", _date("2027-10-28")),
    ])
    root = write_bundle(payload, tmp_path)
    later = _payload([
        _value("lender", "Gamma", _text("Gamma"), instance="gamma"),
        _value("lender_commitment", "50", _money("50"), instance="gamma"),
    ], revision=2, effective_date=date(2024, 1, 15))
    write_bundle(later, tmp_path)
    access, entities = open_access(root.parent.parent.parent)
    top = access.run("top_lenders", {"document_id": "doc", "version_label": "v1", "n": 1})
    assert top["rows"][0]["lender_name"] == "Alpha"
    assert top["row_count"] == 1
    injected = access.run("top_lenders", {
        "document_id": "doc' OR '1'='1", "version_label": "v1", "n": 5,
    })
    assert injected["rows"] == []
    with pytest.raises(QueryError) as caught:
        access.run("drop_table", {})
    assert caught.value.status == 404
    periods = access.run("commitments_by_period", {"document_id": "doc", "grain": "year"})
    assert {row["period"]: row["sum"] for row in periods["rows"]} == {"2022": "400", "2024": "50"}
    facts = access.run("field_facts", {"document_id": "doc", "version_label": "v1", "term_key": "maturity_date"})
    assert [row["date_value"] for row in facts["rows"]] == ["2027-10-28"]
    assert len(entities) == 2

    extra = tmp_path / "queries"
    extra.mkdir()
    (extra / "top_year.json").write_text(json.dumps({
        "name": "top_year",
        "description": "Busiest commitment year, added as a file.",
        "product": "lender_commitments",
        "params": [
            {"name": "document_id", "type": "text"},
            {"name": "grain", "type": "text"},
            {"name": "n", "type": "integer"},
        ],
        "sql": "SELECT effective_date, commitment_amount FROM lender_commitments WHERE document_id = :document_id",
        "time_agg": {"time_field": "effective_date", "grain_param": "grain", "sum_field": "commitment_amount"},
        "top_n": {"n_param": "n", "order_by": "sum", "descending": True},
    }))
    added = AccessLayer(*_products_and_register(tmp_path, extra))
    best = added.run("top_year", {"document_id": "doc", "grain": "year", "n": 1})
    assert best["rows"][0]["period"] == "2022"
    assert best["rows"][0]["sum"] == "400"


def _products_and_register(root, extra):
    from credit_extract.products.model import load_product_dir
    products, _ = load_product_dir(root)
    return products, load_register(extra)


def test_rest_and_mcp_call_the_same_register(tmp_path):
    payload = _payload([
        _value("lender", "Alpha", _text("Alpha"), instance="alpha"),
        _value("lender_commitment", "300", _money("300"), instance="alpha"),
        _value("total_commitments", "300", _money("300")),
    ])
    write_bundle(payload, tmp_path)
    access, entities = open_access(tmp_path)
    status, body = dispatch("GET", "/queries", b"", access, entities)
    assert status == 200 and "top_lenders" in {q["name"] for q in body["queries"]}
    status, body = dispatch(
        "POST", "/queries/top_lenders",
        json.dumps({"params": {"document_id": "doc", "version_label": "v1", "n": 1}}).encode(),
        access, entities,
    )
    assert status == 200 and body["rows"][0]["commitment_amount"] == 300
    status, missing = dispatch("POST", "/queries/top_lenders", b"{}", access, entities)
    assert status == 400 and "document_id" in missing["error"]
    status, _ = dispatch("GET", "/nope", b"", access, entities)
    assert status == 404
    status, products = dispatch("GET", "/products/lender_commitments", b"", access, entities)
    assert status == 200 and products["rows"][0]["commitment_amount"] == "300"

    listed = handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, access, entities)
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert {"top_lenders", "list_products", "get_entity_model"} <= names
    called = handle_rpc({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "facility_terms", "arguments": {"document_id": "doc", "version_label": "v1"}},
    }, access, entities)
    text = json.loads(called["result"]["content"][0]["text"])
    assert text["rows"][0]["term_key"] == "total_commitments"
    assert handle_rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, access, entities) is None

    stream = BytesIO()
    write_message(stream, {"jsonrpc": "2.0", "id": 7, "method": "ping"})
    stream.seek(0)
    assert read_message(stream)["method"] == "ping"


def test_albemarle_handoff_becomes_an_entity_model():
    payload = HandoffPayload.model_validate_json(HANDOFF.read_text())
    products = build_products(payload)
    model = build_entity_model(payload, products)
    terms = {row["term_key"]: row for row in products["facility_terms"]["rows"] if not row["conflict"]}
    assert Decimal(terms["total_commitments"]["amount"]) == Decimal("1500000000")
    assert terms["maturity_date"]["date_value"] == "2027-10-28"
    borrowers = {row["instance"] for row in products["party_roles"]["rows"] if row["role"] == "borrower"}
    assert borrowers == {"albemarle_corporation", "albemarle_europe_srl"}
    boa = next(row for row in products["lender_commitments"]["rows"] if row["instance"] == "bank_of_america_n_a")
    assert Decimal(boa["commitment_amount"]) == Decimal("170000000")
    assert boa["lender_name"] == "Bank of America, N.A."
    assert model["source"]["withheld_pending_review"] == 53
    assert len(products["field_facts"]["rows"]) == len(payload.values)
    assert {row["status"] for row in products["field_facts"]["rows"]} <= {"auto_accepted", "confirmed", "corrected"}
    bank = next(e for e in model["entities"] if e["id"] == "party:albemarle:bank_of_america_n_a")
    assert set(bank["attributes"]["roles"]) >= {"lender", "administrative_agent"}
