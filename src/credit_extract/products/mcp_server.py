"""MCP stdio server. Tools are the query register plus read-only product and entity lookups.

Framing is Content-Length, the same framing MCP clients use on stdin/stdout.
"""

import json
import sys

from .. import __version__
from .access import AccessLayer, QueryError

PROTOCOL = "2024-11-05"


def handle_rpc(message: dict, access: AccessLayer, entities: list[dict]) -> dict | None:
    method = message.get("method")
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    req_id = message.get("id")
    try:
        result = _dispatch(method, message.get("params") or {}, access, entities)
    except QueryError as exc:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(exc)}}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(exc)}}
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def serve_stdio(access: AccessLayer, entities: list[dict], stdin=None, stdout=None) -> None:
    reader = stdin if stdin is not None else sys.stdin.buffer
    writer = stdout if stdout is not None else sys.stdout.buffer
    while True:
        message = read_message(reader)
        if message is None:
            return
        response = handle_rpc(message, access, entities)
        if response is not None:
            write_message(writer, response)


def read_message(stream) -> dict | None:
    headers = {}
    while True:
        line = stream.readline()
        if line == b"":
            return None
        if line in (b"\r\n", b"\n"):
            break
        if b":" in line:
            key, value = line.decode().split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    return json.loads(stream.read(length))


def write_message(stream, message: dict) -> None:
    body = json.dumps(message).encode()
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    stream.flush()


def _dispatch(method: str | None, params: dict, access: AccessLayer, entities: list[dict]):
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "credit-extract", "version": __version__},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": _tools(access)}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        return {"content": [{"type": "text", "text": json.dumps(_call(name, arguments, access, entities), indent=2)}],
                "isError": False}
    raise QueryError(f"unknown method {method!r}")


def _tools(access: AccessLayer) -> list[dict]:
    tools = [
        _tool("list_products", "List JSON data products loaded on this server.", {}),
        _tool("get_entity_model", "Return the entity-relationship model for the loaded agreements.", {
            "document_id": {"type": "string", "description": "Optional filter"},
        }),
    ]
    for spec in access.queries.values():
        properties = {}
        required = []
        for param in spec.params:
            kind = {"text": "string", "integer": "integer", "real": "number"}[param.type]
            properties[param.name] = {"type": kind, "description": param.description}
            if param.required:
                required.append(param.name)
        tools.append(_tool(spec.name, spec.description, properties, required))
    return tools


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return {"name": name, "description": description, "inputSchema": schema}


def _call(name: str | None, arguments: dict, access: AccessLayer, entities: list[dict]):
    if name == "list_products":
        return [{"name": n, "grain": d.get("grain"), "rows": len(d["rows"])} for n, d in sorted(access.products.items())]
    if name == "get_entity_model":
        wanted = arguments.get("document_id")
        models = entities if not wanted else [m for m in entities if m.get("document_id") == wanted]
        return {"entity_models": models}
    if name in access.queries:
        return access.run(name, arguments)
    raise QueryError(f"unknown tool {name!r}", status=404)

