"""MCP orchestration layer built on the official MCP Python SDK.

The application deliberately delegates multi-server connection lifecycle,
tool aggregation, name collision handling, and tool dispatch to
mcp.ClientSessionGroup instead of maintaining a parallel custom transport
orchestration implementation.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from contextvars import ContextVar
from typing import Any

from mcp import Client, ClientSessionGroup
from mcp.client.session_group import (
    SseServerParameters,
    StreamableHttpParameters,
)
from mcp_registry import load_server_configs, tool_allowed

logger = logging.getLogger(__name__)

_DISCOVERY_CACHE = {"key": None, "expires_at": 0.0, "schemas": []}
_LAST_DIAGNOSTICS = []
_CURRENT_SERVER_NAME: ContextVar[str] = ContextVar("mcp_server_name", default="")


def _safe_tool_component(value):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(value)).strip("_") or "tool"


def _timeout_seconds():
    try:
        value = float(os.getenv("MCP_TIMEOUT_SECONDS", "30"))
    except ValueError:
        value = 30.0
    return max(1.0, min(value, 120.0))


def _registry_key():
    try:
        from connectors import list_connectors

        personal = list_connectors()
        personal_key = repr(
            [
                (
                    x["id"],
                    x["name"],
                    x["transport"],
                    x["url"],
                    x.get("headers", {}),
                    x["allowed_tools"],
                    x["enabled"],
                )
                for x in personal
            ]
        )
    except Exception:
        personal_key = ""
    return f"{os.getenv('MCP_SERVERS', '').strip()}|{personal_key}"


def _component_name(tool_name, _server_info):
    server_name = _CURRENT_SERVER_NAME.get() or getattr(_server_info, "name", None) or "server"
    return f"{_safe_tool_component(server_name)}__{_safe_tool_component(tool_name)}"


def _server_params(config):
    timeout = _timeout_seconds()
    if config.transport not in {"streamable-http", "sse"}:
        raise ValueError(f"Unsupported MCP transport: {config.transport}")
    if not config.url:
        raise ValueError(f"MCP server {config.name!r} has no URL.")

    # Re-validate at connect time (not just at registration time) so a
    # connector whose DNS record changes after being saved (DNS rebinding)
    # can't be used to reach a private/local address later.
    from connectors import validate_connector_url

    try:
        validate_connector_url(config.url, resolve_dns=True)
    except ValueError as exc:
        raise ValueError(f"MCP server {config.name!r} blocked at connect time: {exc}") from exc

    if config.transport == "streamable-http":
        return StreamableHttpParameters(
            url=config.url,
            headers=config.headers or {},
            timeout=timeout,
            sse_read_timeout=max(timeout, 300.0),
            terminate_on_close=True,
        )
    return SseServerParameters(
        url=config.url,
        headers=config.headers or {},
        timeout=timeout,
        sse_read_timeout=max(timeout, 300.0),
    )


def _groq_json_schema(schema):
    """Normalize MCP JSON Schema to the subset accepted by function calling."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    allowed = {"type", "properties", "required", "description", "enum", "items", "additionalProperties"}
    out = {key: value for key, value in schema.items() if key in allowed}
    if out.get("type") != "object":
        out = {"type": "object", "properties": {}}
    props = out.get("properties")
    if not isinstance(props, dict):
        out["properties"] = {}
    else:
        out["properties"] = {
            str(name): _groq_json_schema(value) if isinstance(value, dict) and value.get("type") == "object"
            else {key: value for key, value in value.items() if key in allowed} if isinstance(value, dict)
            else {"type": "string"}
            for name, value in props.items()
        }
    required = out.get("required")
    if isinstance(required, list):
        out["required"] = [str(x) for x in required if str(x) in out["properties"]]
    else:
        out.pop("required", None)
    return out


def _schemas_from_group(group, configs_by_name):
    schemas = []
    for qualified_name, tool in group.tools.items():
        if "__" not in qualified_name:
            continue
        server_name, _ = qualified_name.split("__", 1)
        config = configs_by_name.get(server_name)
        if config is None:
            continue
        if not tool_allowed(config, tool.name):
            continue
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
        schema = _groq_json_schema(schema or {"type": "object", "properties": {}})
        name = f"mcp__{qualified_name}"
        if len(name) > 64:
            logger.warning("Skipping MCP tool with name longer than 64 characters: %s", name)
            continue
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": (getattr(tool, "description", None) or f"MCP tool {tool.name}")[:4096],
                    "parameters": schema,
                },
            }
        )
    return schemas


async def _discover_group(configs):
    diagnostics = []
    schemas = []
    configs_by_name = {config.name: config for config in configs}

    async with ClientSessionGroup(component_name_hook=_component_name) as group:
        for config in configs:
            token = _CURRENT_SERVER_NAME.set(config.name)
            try:
                await group.connect_to_server(_server_params(config))
            except Exception as exc:
                diagnostics.append({"name": config.name, "error": str(exc).strip()[:500] or exc.__class__.__name__})
                logger.warning("MCP connection failed for %s: %s", config.name, exc)
            finally:
                _CURRENT_SERVER_NAME.reset(token)

        schemas = _schemas_from_group(group, configs_by_name)
    return schemas, diagnostics


async def _discover_one(config):
    schemas, diagnostics = await _discover_group([config])
    if diagnostics:
        return {
            "connected": False,
            "tools": 0,
            "tool_names": [],
            "error": diagnostics[0]["error"],
            "server": config.name,
        }
    tool_names = [x["function"]["name"] for x in schemas]
    return {
        "connected": True,
        "tools": len(tool_names),
        "tool_names": tool_names,
        "server": config.name,
    }


def discover_connector_diagnostics(server_name):
    config = next((x for x in load_server_configs() if x.name == server_name), None)
    if config is None:
        raise ValueError(f"MCP server {server_name!r} is not configured.")
    try:
        return asyncio.run(asyncio.wait_for(_discover_one(config), timeout=_timeout_seconds()))
    except Exception as exc:
        return {
            "connected": False,
            "tools": 0,
            "tool_names": [],
            "error": str(exc).strip()[:500] or exc.__class__.__name__,
            "server": server_name,
        }


def discover_connector_tool_schemas(server_name):
    diagnostic = discover_connector_diagnostics(server_name)
    if not diagnostic.get("connected"):
        raise RuntimeError(diagnostic.get("error") or "MCP connection failed.")
    config = next((x for x in load_server_configs() if x.name == server_name), None)
    if config is None:
        raise ValueError(f"MCP server {server_name!r} is not configured.")
    schemas, diagnostics = asyncio.run(
        asyncio.wait_for(_discover_group([config]), timeout=_timeout_seconds())
    )
    if diagnostics:
        raise RuntimeError(diagnostics[0]["error"])
    return schemas


def discover_tool_schemas():
    key = _registry_key()
    now = __import__("time").monotonic()
    try:
        ttl = float(os.getenv("MCP_DISCOVERY_TTL_SECONDS", "60"))
    except ValueError:
        ttl = 60.0
    ttl = max(1.0, min(ttl, 600.0))
    if _DISCOVERY_CACHE["key"] == key and now < _DISCOVERY_CACHE["expires_at"]:
        return list(_DISCOVERY_CACHE["schemas"])

    schemas, diagnostics = asyncio.run(
        asyncio.wait_for(_discover_group(load_server_configs()), timeout=_timeout_seconds() * max(1, len(load_server_configs())))
    )
    _LAST_DIAGNOSTICS[:] = diagnostics
    _DISCOVERY_CACHE.update(key=key, expires_at=now + ttl, schemas=schemas)
    return list(schemas)


def _content_to_text(result):
    prefix = "MCP tool error" if bool(getattr(result, "is_error", getattr(result, "isError", False))) else "MCP tool result"
    parts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        parts.append(text if text is not None else str(item))
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if structured is not None:
        parts.append(str(structured))
    return prefix + ": " + ("\n".join(parts) if parts else "(empty)")


async def _call_group_tool(name, arguments):
    if not name.startswith("mcp__"):
        raise ValueError("Not an MCP tool.")
    qualified = name[len("mcp__") :]
    if "__" not in qualified:
        raise ValueError("Invalid MCP tool name.")
    server_name, original_tool_name = qualified.split("__", 1)

    config = next((x for x in load_server_configs() if _safe_tool_component(x.name) == server_name), None)
    if config is None:
        raise ValueError(f"MCP server {server_name!r} is not configured.")
    if not tool_allowed(config, original_tool_name):
        raise PermissionError(f"MCP tool {original_tool_name!r} is not allowed for server {config.name!r}.")

    async with ClientSessionGroup(component_name_hook=_component_name) as group:
        token = _CURRENT_SERVER_NAME.set(config.name)
        try:
            await group.connect_to_server(_server_params(config))
        finally:
            _CURRENT_SERVER_NAME.reset(token)
        group_name = f"{_safe_tool_component(config.name)}__{_safe_tool_component(original_tool_name)}"
        if group_name not in group.tools:
            raise ValueError(f"MCP tool {original_tool_name!r} is not available on server {config.name!r}.")
        result = await group.call_tool(group_name, arguments or {}, read_timeout_seconds=_timeout_seconds())
        return _content_to_text(result)


def call_tool(name, arguments):
    return asyncio.run(asyncio.wait_for(_call_group_tool(name, arguments), timeout=_timeout_seconds()))
