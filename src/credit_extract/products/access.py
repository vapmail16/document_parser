"""Access layer. Loads JSON products into SQLite and runs a registered SQL template.

Callers pass a query name and parameters. The SQL text comes only from the register.
After the template filters rows, declared query-layer operators (top_n, time aggregation) run.
"""

import json
import sqlite3
import threading
from pathlib import Path

from .model import load_product_dir
from .query_layer import top_n, time_aggregate
from .register import QuerySpec, load_register

_SQL_TYPE = {"text": "TEXT", "integer": "INTEGER", "real": "REAL"}


class QueryError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class AccessLayer:
    def __init__(self, products: dict[str, dict], register: list[QuerySpec]):
        self.products = products
        self.queries = {q.name: q for q in register}
        # HTTP serves each request on its own thread. One connection is shared; the lock
        # keeps those calls from interleaving.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        for name, product in products.items():
            _load_table(self._conn, name, product)

    def catalog(self) -> list[dict]:
        return [
            {
                "name": q.name,
                "description": q.description,
                "product": q.product,
                "params": [p.model_dump() for p in q.params],
                "operators": [name for name, op in (("top_n", q.top_n), ("time_agg", q.time_agg)) if op],
                "sql": q.sql,
            }
            for q in self.queries.values()
        ]

    def run(self, name: str, params: dict | None = None) -> dict:
        spec = self.queries.get(name)
        if spec is None:
            raise QueryError(f"unknown query {name!r}", status=404)
        if spec.product not in self.products:
            raise QueryError(f"query {name!r} reads product {spec.product!r}, which is not loaded", status=404)
        bound = _bind(spec, params or {})
        try:
            with self._lock:
                cursor = self._conn.execute(spec.sql, bound)
                fetched = cursor.fetchall()
        except sqlite3.Error as exc:
            raise QueryError(f"query {name!r} failed: {exc}") from exc
        rows = [_jsonify(dict(row)) for row in fetched]
        skipped = 0
        if spec.time_agg:
            grain = bound[spec.time_agg.grain_param]
            try:
                rows, skipped = time_aggregate(rows, spec.time_agg.time_field, str(grain), spec.time_agg.sum_field)
            except ValueError as exc:
                raise QueryError(str(exc)) from exc
        if spec.top_n:
            try:
                n = int(bound[spec.top_n.n_param])
                rows = top_n(rows, n, spec.top_n.order_by, spec.top_n.descending)
            except (TypeError, ValueError) as exc:
                raise QueryError(f"n must be an integer >= 1 ({exc})") from exc
        return {"query": name, "rows": rows, "row_count": len(rows), "skipped_null_time": skipped}


def open_access(root: Path, extra_queries: Path | None = None) -> tuple["AccessLayer", list[dict]]:
    products, entities = load_product_dir(root)
    return AccessLayer(products, load_register(extra_queries)), entities


def _bind(spec: QuerySpec, params: dict) -> dict:
    bound = {}
    for param in spec.params:
        if param.name not in params or params[param.name] is None or params[param.name] == "":
            if param.required:
                raise QueryError(f"missing parameter {param.name!r}")
            bound[param.name] = None
            continue
        bound[param.name] = _coerce(param.name, param.type, params[param.name])
    return bound


def _coerce(name: str, kind: str, value):
    try:
        if kind == "integer":
            if isinstance(value, bool):
                raise ValueError
            return int(value)
        if kind == "real":
            return float(value)
        return str(value)
    except (TypeError, ValueError) as exc:
        raise QueryError(f"parameter {name!r} must be {kind}") from exc


def _load_table(conn: sqlite3.Connection, name: str, product: dict) -> None:
    columns = product["columns"]
    defs = ", ".join(f"{c['name']} {_SQL_TYPE[c['type']]}" for c in columns)
    conn.execute(f"CREATE TABLE {name} ({defs})")
    if not product["rows"]:
        return
    names = [c["name"] for c in columns]
    types = {c["name"]: c["type"] for c in columns}
    placeholders = ", ".join("?" for _ in names)
    values = [tuple(_cell(row.get(col), types[col]) for col in names) for row in product["rows"]]
    conn.executemany(f"INSERT INTO {name} ({', '.join(names)}) VALUES ({placeholders})", values)


def _cell(value, kind: str):
    if value is None or value == "":
        return None
    if kind == "integer":
        return int(value)
    if kind == "real":
        return float(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _jsonify(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        out[key] = value
    return out
