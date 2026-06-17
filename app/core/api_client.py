"""API client generator (Milestone 4 §7, secondary/light).

Derived from the **OpenAPI document** (not the schema) — keeping the milestone's rule that everything
downstream flows from the single source of truth. Deterministic and LLM-free; snapshot-tested, no live
gate (it makes no network calls itself). Produces a tiny ``fetch``-based TypeScript client (one typed
function per operation) and a Postman collection.
"""

from __future__ import annotations

from typing import Any

_METHODS = ("get", "post", "patch", "put", "delete")


def _operations(openapi: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the OpenAPI paths into a deterministic, sorted list of operations."""
    ops: list[dict[str, Any]] = []
    for path in sorted(openapi.get("paths", {})):
        methods = openapi["paths"][path]
        for method in _METHODS:
            op = methods.get(method)
            if not op:
                continue
            params = op.get("parameters", [])
            has_path_id = any(p.get("in") == "path" for p in params)
            query = [p["name"] for p in params if p.get("in") == "query"]
            ops.append({
                "id": op.get("operationId") or f"{method}_{path}",
                "method": method.upper(),
                "path": path,
                "summary": op.get("summary", ""),
                "hasPathId": has_path_id,
                "query": query,
                "hasBody": "requestBody" in op,
            })
    return ops


def _ts_fn(op: dict[str, Any]) -> str:
    args: list[str] = []
    if op["hasPathId"]:
        args.append("id: string")
    if op["hasBody"]:
        args.append("body: unknown")
    if op["query"]:
        args.append("query: Record<string, string | number> = {}")
    arg_sig = ", ".join(args)

    # Build the path expression (substitute {id}, {item_id}).
    path_expr = "`" + op["path"].replace("{id}", "${id}").replace("{item_id}", "${id}") + "`"
    init_parts = [f'method: "{op["method"]}"', "headers: { \"Content-Type\": \"application/json\" }"]
    if op["hasBody"]:
        init_parts.append("body: JSON.stringify(body)")
    init = "{ " + ", ".join(init_parts) + " }"
    query_line = ""
    url_expr = "this.baseUrl + " + path_expr
    if op["query"]:
        query_line = "    const qs = new URLSearchParams(query as Record<string, string>).toString();\n"
        url_expr = "this.baseUrl + " + path_expr + ' + (qs ? "?" + qs : "")'

    return (
        f"  /** {op['summary']} */\n"
        f"  async {_camel(op['id'])}({arg_sig}): Promise<Response> {{\n"
        f"{query_line}"
        f"    return fetch({url_expr}, {init});\n"
        f"  }}\n"
    )


def _camel(name: str) -> str:
    parts = [p for p in name.replace("/", "_").replace("-", "_").split("_") if p]
    if not parts:
        return "op"
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def _typescript_client(openapi: dict[str, Any]) -> str:
    ops = _operations(openapi)
    title = openapi.get("info", {}).get("title", "Generated API")
    body = "\n".join(_ts_fn(op) for op in ops)
    return (
        f"// {title} — generated TypeScript client (Visual Database Designer, Milestone 4).\n"
        "// Source of truth: the OpenAPI document. Do not edit by hand.\n\n"
        "export class ApiClient {\n"
        "  constructor(private baseUrl: string = \"\") {}\n\n"
        f"{body}"
        "}\n"
    )


def _postman_collection(openapi: dict[str, Any]) -> dict[str, Any]:
    info = openapi.get("info", {})
    items = []
    for op in _operations(openapi):
        url_path = op["path"].replace("{id}", ":id").replace("{item_id}", ":id")
        request: dict[str, Any] = {
            "method": op["method"],
            "header": [{"key": "Content-Type", "value": "application/json"}],
            "url": {"raw": "{{baseUrl}}" + url_path,
                    "host": ["{{baseUrl}}"], "path": [p for p in url_path.split("/") if p]},
        }
        if op["hasBody"]:
            request["body"] = {"mode": "raw", "raw": "{}"}
        items.append({"name": op["summary"] or op["id"], "request": request})
    return {
        "info": {"name": info.get("title", "Generated API"),
                 "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
        "variable": [{"key": "baseUrl", "value": "http://localhost:8000"}],
        "item": items,
    }


def generate_client(openapi: dict[str, Any], *, target: str = "typescript") -> dict[str, Any]:
    """Generate a client + Postman collection from an OpenAPI document (deterministic)."""
    if target not in {"typescript", "ts"}:
        raise ValueError(f"unsupported client target {target!r} (Milestone 4 supports 'typescript')")
    return {
        "files": {"client.ts": _typescript_client(openapi)},
        "postman": _postman_collection(openapi),
    }
