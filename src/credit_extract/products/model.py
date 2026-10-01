"""Build an entity-relationship model and JSON data products from a handoff payload.

The handoff is already limited to accepted values. This module does not re-score them.
Party identity is the extraction instance key when one exists, otherwise a slug of the
printed name. Names that only differ by case share an id; names that differ in words do not.
"""

import json
import re
from decimal import Decimal
from pathlib import Path

from ..handoff import HandoffPayload, HandoffValue

PARTY_ROLES = {"borrower", "lender", "administrative_agent", "guarantor"}
COMMITMENT_KEY = "lender_commitment"

_SLUG = re.compile(r"[^a-z0-9]+")


def build_products(payload: HandoffPayload) -> dict[str, dict]:
    """Four products. field_facts keeps every accepted value; the others are business grains."""
    facts = [_fact_row(payload, v) for v in payload.values]
    terms = _facility_terms(payload, [f for f in facts if f["term_key"] not in PARTY_ROLES | {COMMITMENT_KEY}])
    parties = _party_roles(payload, [f for f in facts if f["term_key"] in PARTY_ROLES])
    commitments = _commitments(payload, facts, parties)
    return {
        "field_facts": _product(
            "field_facts",
            "One row per accepted extracted value. Not collapsed. The grain counts and time aggregations use.",
            _FACT_COLUMNS, facts,
        ),
        "facility_terms": _product(
            "facility_terms",
            "One row per facility-level term on one agreement version. Equal values collapse; disagreements stay.",
            _TERM_COLUMNS, terms,
        ),
        "party_roles": _product(
            "party_roles",
            "One row per party per role. party_id is shared across roles of the same instance.",
            _PARTY_COLUMNS, parties,
        ),
        "lender_commitments": _product(
            "lender_commitments",
            "One row per lender commitment on one agreement version.",
            _COMMIT_COLUMNS, commitments,
        ),
    }


def build_entity_model(payload: HandoffPayload, products: dict[str, dict] | None = None) -> dict:
    products = products or build_products(payload)
    facility_id = f"facility:{payload.document_id}:{payload.version_label}"
    entities = [{
        "id": facility_id,
        "type": "Facility",
        "attributes": {
            "document_id": payload.document_id,
            "version_label": payload.version_label,
            "revision": payload.revision,
            "effective_date": _iso(payload.effective_date),
            "execution_status": payload.execution_status,
            "document_kind": payload.document_kind,
        },
    }]
    relationships = []

    by_party: dict[str, dict] = {}
    for row in products["party_roles"]["rows"]:
        party = by_party.setdefault(row["party_id"], {
            "id": row["party_id"], "type": "Party",
            "attributes": {"name": row["name"], "names": [], "roles": [], "instance": row["instance"]},
        })
        attrs = party["attributes"]
        if row["name"] not in attrs["names"]:
            attrs["names"].append(row["name"])
        if row["role"] not in attrs["roles"]:
            attrs["roles"].append(row["role"])
        if len(row["name"]) > len(attrs["name"]):
            attrs["name"] = row["name"]
        relationships.append({
            "type": "plays_role", "from": row["party_id"], "to": facility_id,
            "attributes": {"role": row["role"], "version_label": payload.version_label, "page": row["page"]},
        })
    for party in by_party.values():
        party["attributes"]["name_conflict"] = len({n.casefold() for n in party["attributes"]["names"]}) > 1
        entities.append(party)

    for row in products["lender_commitments"]["rows"]:
        commitment_id = f"commitment:{payload.document_id}:{payload.version_label}:{row['instance']}"
        entities.append({
            "id": commitment_id,
            "type": "Commitment",
            "attributes": {
                "lender_name": row["lender_name"],
                "amount": row["commitment_amount"],
                "page": row["page"],
                "quote": row["quote"],
            },
        })
        relationships.append({
            "type": "commits", "from": row["party_id"], "to": commitment_id,
            "attributes": {"amount": row["commitment_amount"]},
        })
        relationships.append({
            "type": "under_facility", "from": commitment_id, "to": facility_id,
            "attributes": {"version_label": payload.version_label},
        })

    for row in products["facility_terms"]["rows"]:
        term_id = f"term:{payload.document_id}:{payload.version_label}:{row['term_key']}"
        if row["conflict"]:
            term_id += f":p{row['page']}"
        entities.append({
            "id": term_id,
            "type": "Term",
            "attributes": {
                "key": row["term_key"],
                "value_type": row["value_type"],
                "amount": row["amount"],
                "date_value": row["date_value"],
                "text_value": row["text_value"],
                "page": row["page"],
                "quote": row["quote"],
                "conflict": bool(row["conflict"]),
            },
        })
        relationships.append({"type": "has_term", "from": facility_id, "to": term_id, "attributes": {"key": row["term_key"]}})

    entities.sort(key=lambda e: e["id"])
    relationships.sort(key=lambda r: (r["type"], r["from"], r["to"], json.dumps(r["attributes"], sort_keys=True)))
    return {
        "kind": "entity_model",
        "document_id": payload.document_id,
        "version_label": payload.version_label,
        "revision": payload.revision,
        "effective_date": _iso(payload.effective_date),
        "entities": entities,
        "relationships": relationships,
        "source": {
            "run_id": payload.provenance.run_id,
            "dictionary_version": payload.provenance.dictionary_version,
            "model_id": payload.provenance.model_id,
            "withheld_pending_review": payload.withheld_pending_review,
            "accepted_values": len(payload.values),
        },
    }


def write_bundle(payload: HandoffPayload, root: Path) -> Path:
    """Write the entity model and each product as JSON. Existing files for this revision are replaced."""
    products = build_products(payload)
    model = build_entity_model(payload, products)
    dest = root / payload.document_id / payload.version_label / f"r{payload.revision:03d}"
    dest.mkdir(parents=True, exist_ok=True)
    _dump(dest / "entity_model.json", model)
    for name, product in products.items():
        _dump(dest / f"{name}.json", product)
    _dump(dest / "manifest.json", {
        "kind": "bundle",
        "document_id": payload.document_id,
        "version_label": payload.version_label,
        "revision": payload.revision,
        "files": ["entity_model.json", *[f"{name}.json" for name in sorted(products)]],
    })
    return dest


def load_product_dir(root: Path) -> tuple[dict[str, dict], list[dict]]:
    """Merge every data product under root. Same product name across revisions concatenates rows."""
    products: dict[str, dict] = {}
    entities: list[dict] = []
    if not root.exists():
        return products, entities
    for path in sorted(root.rglob("*.json")):
        doc = json.loads(path.read_text())
        kind = doc.get("kind")
        if kind == "data_product":
            current = products.get(doc["name"])
            if current is None:
                products[doc["name"]] = doc
            else:
                known = {c["name"] for c in current["columns"]}
                current["columns"].extend(c for c in doc["columns"] if c["name"] not in known)
                current["rows"].extend(doc["rows"])
        elif kind == "entity_model":
            entities.append(doc)
    return products, entities


# --- grains -----------------------------------------------------------------------

_FACT_COLUMNS = [
    ("document_id", "text"), ("version_label", "text"), ("revision", "integer"),
    ("effective_date", "text"), ("term_key", "text"), ("instance", "text"),
    ("value_type", "text"), ("amount", "real"), ("currency", "text"),
    ("date_value", "text"), ("text_value", "text"), ("page", "integer"),
    ("quote", "text"), ("grounding", "text"), ("status", "text"), ("origin", "text"),
]
_TERM_COLUMNS = [
    ("document_id", "text"), ("version_label", "text"), ("revision", "integer"),
    ("effective_date", "text"), ("term_key", "text"), ("value_type", "text"),
    ("amount", "real"), ("date_value", "text"), ("text_value", "text"),
    ("page", "integer"), ("also_pages", "text"), ("quote", "text"),
    ("status", "text"), ("conflict", "integer"),
]
_PARTY_COLUMNS = [
    ("document_id", "text"), ("version_label", "text"), ("revision", "integer"),
    ("effective_date", "text"), ("party_id", "text"), ("role", "text"),
    ("instance", "text"), ("name", "text"), ("page", "integer"), ("quote", "text"), ("status", "text"),
]
_COMMIT_COLUMNS = [
    ("document_id", "text"), ("version_label", "text"), ("revision", "integer"),
    ("effective_date", "text"), ("party_id", "text"), ("lender_name", "text"),
    ("instance", "text"), ("commitment_amount", "real"), ("page", "integer"),
    ("quote", "text"), ("status", "text"), ("conflict", "integer"),
]


def _product(name: str, grain: str, columns: list[tuple[str, str]], rows: list[dict]) -> dict:
    return {
        "kind": "data_product",
        "name": name,
        "grain": grain,
        "columns": [{"name": n, "type": t} for n, t in columns],
        "rows": rows,
    }


def _fact_row(payload: HandoffPayload, value: HandoffValue) -> dict:
    amount, date_value, text_value, currency = _slots(value)
    return {
        "document_id": payload.document_id,
        "version_label": payload.version_label,
        "revision": payload.revision,
        "effective_date": _iso(payload.effective_date),
        "term_key": value.key,
        "instance": value.instance,
        "value_type": value.value.type.value if value.value else None,
        "amount": amount,
        "currency": currency,
        "date_value": date_value,
        "text_value": text_value,
        "page": value.page,
        "quote": value.quote,
        "grounding": value.grounding,
        "status": value.status,
        "origin": value.origin,
    }


def _facility_terms(payload: HandoffPayload, facts: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for fact in facts:
        groups.setdefault((fact["term_key"], _value_key(fact)), []).append(fact)
    by_key: dict[str, list[tuple]] = {}
    for group_key, rows in groups.items():
        by_key.setdefault(group_key[0], []).append(group_key)
    out = []
    for (term_key, _), rows in sorted(groups.items(), key=lambda item: (item[0][0], item[1][0]["page"])):
        first = min(rows, key=lambda r: r["page"])
        pages = sorted({r["page"] for r in rows})
        conflict = len(by_key[term_key]) > 1
        out.append({
            "document_id": payload.document_id,
            "version_label": payload.version_label,
            "revision": payload.revision,
            "effective_date": _iso(payload.effective_date),
            "term_key": term_key,
            "value_type": first["value_type"],
            "amount": first["amount"],
            "date_value": first["date_value"],
            "text_value": first["text_value"],
            "page": first["page"],
            "also_pages": ",".join(str(p) for p in pages if p != first["page"]),
            "quote": first["quote"],
            "status": first["status"],
            "conflict": int(conflict),
        })
    return out


def _party_roles(payload: HandoffPayload, facts: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for fact in facts:
        if not fact["text_value"]:
            continue
        instance = fact["instance"] or _slug(fact["text_value"])
        groups.setdefault((fact["term_key"], instance, fact["text_value"].casefold()), []).append(fact)
    out = []
    for (role, instance, _), rows in sorted(groups.items()):
        first = min(rows, key=lambda r: (r["page"], -len(r["text_value"])))
        out.append({
            "document_id": payload.document_id,
            "version_label": payload.version_label,
            "revision": payload.revision,
            "effective_date": _iso(payload.effective_date),
            "party_id": f"party:{payload.document_id}:{instance}",
            "role": role,
            "instance": instance,
            "name": max(rows, key=lambda r: len(r["text_value"]))["text_value"],
            "page": first["page"],
            "quote": first["quote"],
            "status": first["status"],
        })
    return out


def _commitments(payload: HandoffPayload, facts: list[dict], parties: list[dict]) -> list[dict]:
    names: dict[str, dict] = {}
    for party in parties:
        if party["role"] != "lender":
            continue
        current = names.get(party["instance"])
        if current is None or len(party["name"]) > len(current["name"]):
            names[party["instance"]] = party
    groups: dict[tuple, list[dict]] = {}
    for fact in facts:
        if fact["term_key"] != COMMITMENT_KEY or fact["amount"] is None or not fact["instance"]:
            continue
        groups.setdefault((fact["instance"], fact["amount"]), []).append(fact)
    distinct = {}
    for instance, amount in groups:
        distinct.setdefault(instance, set()).add(amount)
    out = []
    for (instance, amount), rows in sorted(groups.items()):
        first = min(rows, key=lambda r: r["page"])
        lender = names.get(instance)
        out.append({
            "document_id": payload.document_id,
            "version_label": payload.version_label,
            "revision": payload.revision,
            "effective_date": _iso(payload.effective_date),
            "party_id": f"party:{payload.document_id}:{instance}",
            "lender_name": lender["name"] if lender else None,
            "instance": instance,
            "commitment_amount": amount,
            "page": first["page"],
            "quote": first["quote"],
            "status": first["status"],
            "conflict": int(len(distinct[instance]) > 1),
        })
    return out


def _slots(value: HandoffValue) -> tuple:
    v = value.value
    if v is None:
        return None, None, None, None
    amount = format(Decimal(v.amount).normalize(), "f") if v.amount is not None else None
    return amount, v.date_value.isoformat() if v.date_value else None, v.text, v.currency


def _value_key(fact: dict) -> tuple:
    amount = Decimal(fact["amount"]) if fact["amount"] is not None else None
    return (fact["value_type"], amount, fact["date_value"], fact["text_value"])


def _slug(text: str) -> str:
    return _SLUG.sub("_", text.casefold()).strip("_")[:80] or "unknown"


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _dump(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, indent=2) + "\n")
