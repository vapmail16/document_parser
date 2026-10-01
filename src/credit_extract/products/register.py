"""Query register. One entry is one question the API is willing to answer.

REST and MCP call a query by name. They never accept SQL. To add a question, append a
QuerySpec here or drop a JSON file of the same shape into a directory and pass that
directory to load_register(). The access layer is the only thing that executes the template.
"""

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_DENIED = re.compile(
    r"\b(insert|update|delete|drop|alter|attach|detach|pragma|replace|create|vacuum|reindex)\b",
    re.IGNORECASE,
)


class Param(BaseModel):
    name: str
    type: Literal["text", "integer", "real"] = "text"
    required: bool = True
    description: str = ""

    @model_validator(mode="after")
    def _name(self) -> "Param":
        if not _IDENT.match(self.name):
            raise ValueError(f"param name {self.name!r} is not an identifier")
        return self


class TopNOp(BaseModel):
    """Applied by the query layer after SQL, using the parameter named by n_param."""

    n_param: str
    order_by: str
    descending: bool = True


class TimeAggOp(BaseModel):
    """Applied by the query layer after SQL. grain_param is year, quarter, or month."""

    time_field: str
    grain_param: str
    sum_field: str | None = None


class QuerySpec(BaseModel):
    name: str
    description: str
    product: str
    params: list[Param] = Field(default_factory=list)
    sql: str
    top_n: TopNOp | None = None
    time_agg: TimeAggOp | None = None

    @model_validator(mode="after")
    def _check(self) -> "QuerySpec":
        if not _IDENT.match(self.name) or not _IDENT.match(self.product):
            raise ValueError("query name and product must be identifiers")
        self.sql = validate_sql(self.sql, self.product)
        names = {p.name for p in self.params}
        if self.top_n and (self.top_n.n_param not in names or not _IDENT.match(self.top_n.order_by)):
            raise ValueError(f"{self.name}: top_n must name a parameter and a column")
        if self.time_agg and (
            self.time_agg.grain_param not in names
            or not _IDENT.match(self.time_agg.time_field)
            or (self.time_agg.sum_field is not None and not _IDENT.match(self.time_agg.sum_field))
        ):
            raise ValueError(f"{self.name}: time_agg must name a grain parameter and columns")
        return self


def validate_sql(sql: str, product: str) -> str:
    """A template may read exactly one registered product. It may not write or stack statements."""
    text = sql.strip().rstrip(";").strip()
    if not text.lower().startswith("select") or ";" in text:
        raise ValueError("a query template must be a single SELECT")
    if _DENIED.search(text):
        raise ValueError("a query template may only read")
    if re.search(r"\bjoin\b", text, re.IGNORECASE):
        raise ValueError("joins are not enabled; add a product grain instead")
    found = re.findall(r"\bfrom\s+([a-zA-Z_][a-zA-Z0-9_]*)", text, re.IGNORECASE)
    if found != [product]:
        raise ValueError(f"template must read only {product}")
    return text


def load_register(extra_dir: Path | None = None) -> list[QuerySpec]:
    """Built-in queries, then any JSON files in extra_dir. A duplicate name replaces nothing; it errors."""
    specs = list(BUILTIN)
    seen = {s.name for s in specs}
    if extra_dir is None:
        return specs
    for path in sorted(extra_dir.glob("*.json")):
        spec = QuerySpec.model_validate(json.loads(path.read_text()))
        if spec.name in seen:
            raise ValueError(f"query {spec.name!r} is already registered ({path.name})")
        seen.add(spec.name)
        specs.append(spec)
    return specs


BUILTIN: list[QuerySpec] = [
    QuerySpec(
        name="facility_terms",
        description="Accepted facility-level terms for one agreement version.",
        product="facility_terms",
        params=[
            Param(name="document_id", type="text", description="Agreement id"),
            Param(name="version_label", type="text", description="Version label, e.g. 2022-AR"),
        ],
        sql="""
            SELECT document_id, version_label, revision, effective_date, term_key,
                   value_type, amount, date_value, text_value, page, quote, status, conflict
            FROM facility_terms
            WHERE document_id = :document_id AND version_label = :version_label
        """,
    ),
    QuerySpec(
        name="party_roles",
        description="Parties and the role each plays on one agreement version.",
        product="party_roles",
        params=[
            Param(name="document_id", type="text"),
            Param(name="version_label", type="text"),
        ],
        sql="""
            SELECT document_id, version_label, revision, party_id, role, instance, name, page, quote, status
            FROM party_roles
            WHERE document_id = :document_id AND version_label = :version_label
        """,
    ),
    QuerySpec(
        name="lender_commitments",
        description="Lender commitment schedule for one agreement version.",
        product="lender_commitments",
        params=[
            Param(name="document_id", type="text"),
            Param(name="version_label", type="text"),
        ],
        sql="""
            SELECT document_id, version_label, revision, effective_date, party_id, lender_name,
                   instance, commitment_amount, page, quote, status
            FROM lender_commitments
            WHERE document_id = :document_id AND version_label = :version_label
        """,
    ),
    QuerySpec(
        name="top_lenders",
        description="Largest lender commitments on one agreement version. n is applied by the query layer, not in SQL.",
        product="lender_commitments",
        params=[
            Param(name="document_id", type="text"),
            Param(name="version_label", type="text"),
            Param(name="n", type="integer", description="How many lenders to return"),
        ],
        sql="""
            SELECT lender_name, instance, commitment_amount, page, quote
            FROM lender_commitments
            WHERE document_id = :document_id AND version_label = :version_label
        """,
        top_n=TopNOp(n_param="n", order_by="commitment_amount", descending=True),
    ),
    QuerySpec(
        name="commitments_by_period",
        description="Sum of lender commitments bucketed by the agreement effective date. grain is year, quarter, or month.",
        product="lender_commitments",
        params=[
            Param(name="document_id", type="text"),
            Param(name="grain", type="text", description="year, quarter, or month"),
        ],
        sql="""
            SELECT effective_date, commitment_amount
            FROM lender_commitments
            WHERE document_id = :document_id
        """,
        time_agg=TimeAggOp(time_field="effective_date", grain_param="grain", sum_field="commitment_amount"),
    ),
    QuerySpec(
        name="field_facts",
        description="Every accepted extracted value for one version. Pass term_key to keep one dictionary field.",
        product="field_facts",
        params=[
            Param(name="document_id", type="text"),
            Param(name="version_label", type="text"),
            Param(name="term_key", type="text", required=False, description="Dictionary key; omit for all"),
        ],
        sql="""
            SELECT document_id, version_label, revision, effective_date, term_key, instance,
                   value_type, amount, date_value, text_value, page, quote, status
            FROM field_facts
            WHERE document_id = :document_id AND version_label = :version_label
              AND (:term_key IS NULL OR term_key = :term_key)
        """,
    ),
]
