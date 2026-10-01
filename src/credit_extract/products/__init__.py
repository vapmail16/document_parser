"""Entity model, JSON data products, and the query register served by REST and MCP."""

from .access import AccessLayer, QueryError, open_access
from .model import build_entity_model, build_products, write_bundle

__all__ = [
    "AccessLayer",
    "QueryError",
    "build_entity_model",
    "build_products",
    "open_access",
    "write_bundle",
]
