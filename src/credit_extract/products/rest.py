"""REST access to the same query register the MCP server uses. No sockets are required to call dispatch()."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .access import AccessLayer, QueryError


def dispatch(method: str, path: str, body: bytes, access: AccessLayer, entities: list[dict]) -> tuple[int, dict]:
    parsed = urlparse(path)
    parts = [p for p in parsed.path.split("/") if p]
    query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
    try:
        if method == "GET" and parts == ["health"]:
            return 200, {"status": "ok"}
        if method == "GET" and parts == ["queries"]:
            return 200, {"queries": access.catalog()}
        if method == "GET" and parts == ["products"]:
            return 200, {"products": [
                {"name": name, "grain": doc.get("grain"), "rows": len(doc["rows"])}
                for name, doc in sorted(access.products.items())
            ]}
        if method == "GET" and len(parts) == 2 and parts[0] == "products":
            doc = access.products.get(parts[1])
            if doc is None:
                return 404, {"error": f"unknown product {parts[1]!r}"}
            return 200, doc
        if method == "GET" and parts == ["entity-models"]:
            return 200, {"entity_models": _filter_entities(entities, query.get("document_id"))}
        if len(parts) == 2 and parts[0] == "queries" and method in ("GET", "POST"):
            params = query if method == "GET" else {**_body_params(body), **query}
            return 200, access.run(parts[1], params)
    except QueryError as exc:
        return exc.status, {"error": str(exc)}
    except json.JSONDecodeError:
        return 400, {"error": "body must be JSON"}
    return 404, {"error": f"no route {method} {parsed.path}"}


def serve(access: AccessLayer, entities: list[dict], host: str = "127.0.0.1", port: int = 8765) -> None:
    handler = _handler(access, entities)
    server = ThreadingHTTPServer((host, port), handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _handler(access: AccessLayer, entities: list[dict]):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._respond("GET", b"")

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or 0)
            self._respond("POST", self.rfile.read(length) if length else b"")

        def _respond(self, method: str, body: bytes) -> None:
            status, payload = dispatch(method, self.path, body, access, entities)
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler


def _body_params(body: bytes) -> dict:
    if not body:
        return {}
    doc = json.loads(body)
    if not isinstance(doc, dict):
        raise QueryError("JSON body must be an object")
    params = doc.get("params", doc)
    if not isinstance(params, dict):
        raise QueryError("params must be an object")
    return params


def _filter_entities(entities: list[dict], document_id: str | None) -> list[dict]:
    if not document_id:
        return entities
    return [model for model in entities if model.get("document_id") == document_id]
