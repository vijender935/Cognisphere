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
import httpx2
from mcp.client.streamable_http import streamable_http_client
from mcp.client.sse import sse_client
from mcp_registry import load_server_configs, tool_allowed

logger = logging.getLogger(__name__)

_DISCOVERY_CACHE = {"key": None, "expires_at": 0.0, "schemas": []}
_LAST_DIAGNOSTICS = []
_CURRENT_SERVER_NAME: ContextVar[str] = ContextVar("mcp_server_name", default="")
# Per-server cooldowns prevent a remote MCP server from being hammered after
# authentication failures or rate limiting. The cooldown lives only for the
# current process; the persistent tool catalog remains in _DISCOVERY_CACHE.
_DISCOVERY_BACKOFF: dict[str, tuple[float, str]] = {}


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


def _oauth_access_token(config):
    """Return a stored OAuth access token for a persistent connector, if any."""
    if not getattr(config, "connector_id", None):
        return None
    try:
        from mcp_oauth import oauth_access_token
        return oauth_access_token(config.connector_id)
    except Exception:
        return None


def _request_headers(config):
    """Build request headers, including a persisted OAuth bearer token.

    ClientSessionGroup currently accepts static headers rather than an
    httpx.Auth provider, so persisted MCP OAuth tokens must be bridged into
    the header-based ServerParameters path. Direct tool execution uses the
    same headers.
    """
    headers = dict(config.headers or {})
    if not any(str(key).lower() == "authorization" for key in headers):
        token = _oauth_access_token(config)
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def _exception_status_code(exc):
    for candidate in (
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        try:
            if candidate is not None:
                return int(candidate)
        except (TypeError, ValueError):
            pass
    text = str(exc).lower()
    for code in (401, 403, 429):
        if str(code) in text:
            return code
    return None


def _server_backoff(config_name, status_code, error_text):
    if status_code == 401:
        seconds = 300.0
    elif status_code == 429:
        seconds = 600.0
    elif status_code in {403}:
        seconds = 300.0
    else:
        seconds = 30.0
    _DISCOVERY_BACKOFF[config_name] = (
        __import__("time").monotonic() + seconds,
        error_text,
    )


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
            headers=_request_headers(config),
            timeout=timeout,
            sse_read_timeout=max(timeout, 300.0),
            terminate_on_close=True,
        )
    return SseServerParameters(
        url=config.url,
        headers=_request_headers(config),
        timeout=timeout,
        sse_read_timeout=max(timeout, 300.0),
    )


def _groq_json_schema(schema, *, nullable=False):
    """Normalize MCP JSON Schema for Groq tool calling.

    MCP schemas commonly model optional arguments as ordinary primitive types.
    Some Groq tool-calling models emit null for omitted optional arguments.
    Groq validates generated arguments against the supplied schema before
    returning the response, so optional properties must explicitly allow null.
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    allowed = {
        "type", "properties", "required", "description", "enum",
        "items", "additionalProperties", "anyOf", "oneOf",
    }
    out = {key: value for key, value in schema.items() if key in allowed}

    schema_type = out.get("type")
    if isinstance(schema_type, list):
        types = [str(item) for item in schema_type if item]
        if nullable and "null" not in types:
            types.append("null")
        out["type"] = list(dict.fromkeys(types))
    elif isinstance(schema_type, str):
        if nullable and schema_type != "null":
            out["type"] = [schema_type, "null"]
    elif "anyOf" in out or "oneOf" in out:
        variants_key = "anyOf" if isinstance(out.get("anyOf"), list) else "oneOf"
        variants = out.get(variants_key) or []
        out[variants_key] = [
            _groq_json_schema(item, nullable=False) if isinstance(item, dict) else item
            for item in variants
        ]
        if nullable and not any(
            isinstance(item, dict) and item.get("type") == "null"
            for item in out[variants_key]
        ):
            out[variants_key].append({"type": "null"})
    else:
        out["type"] = "object"

    if out.get("type") == "object":
        props = out.get("properties")
        if not isinstance(props, dict):
            out["properties"] = {}
        else:
            required = {
                str(item) for item in (out.get("required") or [])
                if item is not None
            }
            normalized_props = {}
            for name, value in props.items():
                property_name = str(name)
                normalized_props[property_name] = _groq_json_schema(
                    value if isinstance(value, dict) else {"type": "string"},
                    nullable=property_name not in required,
                )
            out["properties"] = normalized_props

        required = out.get("required")
        if isinstance(required, list):
            out["required"] = [
                str(item) for item in required
                if str(item) in out["properties"]
            ]
        else:
            out.pop("required", None)

    items = out.get("items")
    if isinstance(items, dict):
        out["items"] = _groq_json_schema(items, nullable=False)

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
            now = __import__("time").monotonic()
            cooldown = _DISCOVERY_BACKOFF.get(config.name)
            if cooldown and now < cooldown[0]:
                diagnostics.append({
                    "name": config.name,
                    "error": cooldown[1],
                    "backoff": True,
                })
                logger.warning(
                    "Skipping MCP discovery for %s during backoff: %s",
                    config.name,
                    cooldown[1],
                )
                continue
            _DISCOVERY_BACKOFF.pop(config.name, None)
            token = _CURRENT_SERVER_NAME.set(config.name)
            try:
                await group.connect_to_server(_server_params(config))
                _DISCOVERY_BACKOFF.pop(config.name, None)
            except Exception as exc:
                error_text = str(exc).strip()[:500] or exc.__class__.__name__
                status_code = _exception_status_code(exc)
                _server_backoff(config.name, status_code, error_text)
                diagnostics.append({
                    "name": config.name,
                    "error": error_text,
                    "status_code": status_code,
                })
                logger.warning(
                    "MCP connection failed for %s (status=%s, backoff active): %s",
                    config.name,
                    status_code,
                    exc,
                )
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
        ttl = float(os.getenv("MCP_DISCOVERY_TTL_SECONDS", "300"))
    except ValueError:
        ttl = 60.0
    ttl = max(1.0, min(ttl, 600.0))
    if _DISCOVERY_CACHE["key"] == key and now < _DISCOVERY_CACHE["expires_at"]:
        return list(_DISCOVERY_CACHE["schemas"])

    configs = load_server_configs()
    schemas, diagnostics = asyncio.run(
        asyncio.wait_for(
            _discover_group(configs),
            timeout=_timeout_seconds() * max(1, len(configs)),
        )
    )
    _LAST_DIAGNOSTICS[:] = diagnostics

    # Keep the last known-good tool catalog when a server temporarily rejects
    # tools/list (for example a global rate limit). The agent can still execute
    # a previously discovered tool through _call_group_tool(), which no longer
    # performs a second tools/list request.
    if diagnostics and _DISCOVERY_CACHE.get("key") == key:
        stale = list(_DISCOVERY_CACHE.get("schemas", []))
        if stale:
            failed_servers = {str(item.get("name", "")) for item in diagnostics}
            fresh_servers = {
                str(schema.get("function", {}).get("name", "")).removeprefix("mcp__").split("__", 1)[0]
                for schema in schemas
            }
            merged = list(schemas)
            existing_names = {
                schema.get("function", {}).get("name")
                for schema in merged
            }
            for schema in stale:
                full_name = str(schema.get("function", {}).get("name", ""))
                server = full_name.removeprefix("mcp__").split("__", 1)[0]
                if server in failed_servers and server not in fresh_servers and full_name not in existing_names:
                    merged.append(schema)
            schemas = merged
            logger.warning(
                "MCP discovery partially failed; using cached schemas for servers: %s",
                sorted(failed_servers),
            )

    # Only replace the cache with a genuinely usable catalog. This prevents a
    # transient discovery/rate-limit failure from poisoning the 60-second cache
    # with an empty tool list.
    if schemas or not diagnostics:
        _DISCOVERY_CACHE.update(key=key, expires_at=now + ttl, schemas=list(schemas))
    elif _DISCOVERY_CACHE.get("key") != key:
        _DISCOVERY_CACHE.update(key=key, expires_at=now + ttl, schemas=list(schemas))

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
    """Call one MCP tool without doing a fresh tools/list discovery.

    ClientSessionGroup.connect_to_server() performs tool discovery for the
    connected server. That is useful for aggregation, but it is unnecessary
    for an already-selected tool and can trigger server-side rate limits on
    every execution. The v2 Client can call a known tool directly.
    """
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

    timeout = _timeout_seconds()
    if config.transport == "streamable-http":
        http_timeout = httpx2.Timeout(timeout, read=max(timeout, 300.0))
        async with httpx2.AsyncClient(
            headers=_request_headers(config),
            timeout=http_timeout,
        ) as http_client:
            transport = streamable_http_client(
                config.url,
                http_client=http_client,
                terminate_on_close=True,
            )
            async with Client(transport) as client:
                result = await client.call_tool(
                    original_tool_name,
                    arguments or {},
                    read_timeout_seconds=timeout,
                )
    elif config.transport == "sse":
        transport = sse_client(
            config.url,
            headers=config.headers or {},
            timeout=timeout,
            sse_read_timeout=max(timeout, 300.0),
        )
        async with Client(transport) as client:
            result = await client.call_tool(
                original_tool_name,
                arguments or {},
                read_timeout_seconds=timeout,
            )
    else:
        raise ValueError(f"Unsupported MCP transport: {config.transport}")

    return _content_to_text(result)


def call_tool(name, arguments):
    return asyncio.run(asyncio.wait_for(_call_group_tool(name, arguments), timeout=_timeout_seconds()))
