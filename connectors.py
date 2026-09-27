"""Persistent MCP connector registry for the single-user Personal AI Assistant."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import socket
from urllib.parse import urlparse

from config import ensure_directories
from db import connect


def _is_private_or_local(host: str, resolve_dns: bool = True) -> bool:
    host = host.strip("[]").lower()
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or ip.is_reserved
        )
    except ValueError:
        if not resolve_dns:
            return False
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            if os.getenv("ALLOW_LOCAL_MCP", "0") == "1":
                return False
            raise ValueError("Connector host could not be resolved.") from exc
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if (
                ip.is_loopback
                or ip.is_private
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_unspecified
                or ip.is_reserved
            ):
                return True
        return False


def validate_connector_url(url: str, *, resolve_dns: bool = True) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("Connector URL must be a valid http(s) URL.")
    if parsed.username or parsed.password:
        raise ValueError("Connector URL must not contain embedded credentials.")
    allow_local = os.getenv("ALLOW_LOCAL_MCP", "0") == "1"
    if parsed.scheme == "http" and not allow_local:
        raise ValueError("HTTP MCP connectors are disabled; use HTTPS.")
    if _is_private_or_local(parsed.hostname, resolve_dns=resolve_dns) and not allow_local:
        raise ValueError("Connector host resolves to a private or local address.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Connector URL contains an invalid port.") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Connector port is invalid.")
    return parsed.geturl()


def _decode_status(value):
    if isinstance(value, dict):
        return value
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


# --- Header encryption at rest -------------------------------------------------
_FERNET = None


def _get_fernet():
    global _FERNET
    if _FERNET is not None:
        return _FERNET
    key = os.getenv("MCP_HEADER_ENCRYPTION_KEY", "").strip()
    if not key:
        return None
    from cryptography.fernet import Fernet

    try:
        _FERNET = Fernet(key)
    except Exception:
        _FERNET = Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))
    return _FERNET


def _encrypt_headers(header_map: dict) -> str:
    payload = json.dumps(header_map)
    fernet = _get_fernet()
    if fernet is None:
        return payload
    return "enc:" + fernet.encrypt(payload.encode()).decode()


def _decrypt_headers(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, str) and raw.startswith("enc:"):
        fernet = _get_fernet()
        if fernet is None:
            return {}
        try:
            raw = fernet.decrypt(raw[4:].encode()).decode()
        except Exception:
            return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


_LEGACY_MIGRATED = False


def init_connectors_db():
    global _LEGACY_MIGRATED
    ensure_directories()
    with connect() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS mcp_connectors(
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                transport TEXT NOT NULL,
                url TEXT NOT NULL,
                allowed_tools TEXT NOT NULL DEFAULT '[]',
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                headers TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT '{}',
                last_checked_at TIMESTAMP NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        con.execute("ALTER TABLE mcp_connectors ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT '{}'")
        con.execute("ALTER TABLE mcp_connectors ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMP NULL")
    if not _LEGACY_MIGRATED:
        with connect() as con:
            con.execute("DROP INDEX IF EXISTS mcp_connectors_user_id_name_key")
            con.execute("ALTER TABLE mcp_connectors DROP COLUMN IF EXISTS user_id")
        _LEGACY_MIGRATED = True


def _migrate_legacy_schema():
    """Remove legacy user-scoping from an existing PostgreSQL database."""
    with connect() as con:
        con.execute("DROP INDEX IF EXISTS mcp_connectors_user_id_name_key")
        con.execute("ALTER TABLE mcp_connectors DROP COLUMN IF EXISTS user_id")


def list_connectors(*, redact_headers=False):
    init_connectors_db()
    with connect() as con:
        rows = con.execute(
            """SELECT id,name,transport,url,headers,allowed_tools,enabled,status,last_checked_at,created_at
               FROM mcp_connectors ORDER BY name"""
        ).fetchall()

    result = []
    for connector_id, name, transport, url, headers, allowed_tools, enabled, status, last_checked_at, created_at in rows:
        try:
            tools = json.loads(allowed_tools)
        except (json.JSONDecodeError, TypeError):
            tools = []
        header_map = _decrypt_headers(headers)
        result.append(
            {
                "id": connector_id,
                "name": name,
                "transport": transport,
                "url": url,
                "headers": ({k: "***" for k in header_map} if redact_headers else header_map),
                "allowed_tools": tools if isinstance(tools, list) else [],
                "enabled": bool(enabled),
                "status": _decode_status(status),
                "last_checked_at": last_checked_at,
                "created_at": created_at,
            }
        )
    return result


def get_connector_configs():
    return [x for x in list_connectors(redact_headers=False) if x["enabled"]]


def get_connector(connector_id: int):
    return next((x for x in list_connectors(redact_headers=False) if x["id"] == connector_id), None)


def update_connector_status(connector_id: int, status: dict):
    normalized = status if isinstance(status, dict) else {}
    with connect() as con:
        con.execute(
            "UPDATE mcp_connectors SET status=?, last_checked_at=CURRENT_TIMESTAMP WHERE id=?",
            (json.dumps(normalized), connector_id),
        )
    return get_connector(connector_id)


def upsert_connector(name, transport, url, allowed_tools=None, headers=None):
    name, transport, url = name.strip(), transport.strip().lower(), url.strip()
    if not name or len(name) > 80:
        raise ValueError("Connector name must be 1-80 characters.")
    if transport not in {"streamable-http", "sse"}:
        raise ValueError("Transport must be streamable-http or sse.")
    url = validate_connector_url(url)
    tools = [x.strip() for x in (allowed_tools or []) if isinstance(x, str) and x.strip()]
    if len(tools) > 100:
        raise ValueError("Too many allowed tools.")

    header_map = {}
    if isinstance(headers, dict):
        for key, value in headers.items():
            key, value = str(key).strip(), str(value).strip()
            if not key or len(key) > 128 or any(c in key + value for c in "\r\n"):
                raise ValueError("Invalid MCP header.")
            if key.lower() in {"host", "content-length", "transfer-encoding", "connection"}:
                raise ValueError(f"MCP header {key!r} is not allowed.")
            header_map[key] = value
    if len(header_map) > 30:
        raise ValueError("Too many MCP headers.")

    init_connectors_db()
    with connect() as con:
        con.execute(
            """INSERT INTO mcp_connectors(name,transport,url,headers,allowed_tools,enabled,status,last_checked_at)
               VALUES(?,?,?,?,?,TRUE,'{}',NULL)
               ON CONFLICT(name) DO UPDATE SET
                   transport=excluded.transport,
                   url=excluded.url,
                   headers=excluded.headers,
                   allowed_tools=excluded.allowed_tools,
                   enabled=TRUE,
                   status='{}',
                   last_checked_at=NULL""",
            (name, transport, url, _encrypt_headers(header_map), json.dumps(tools)),
        )
    return get_connector(next(x["id"] for x in list_connectors() if x["name"] == name))


def delete_connector(connector_id):
    init_connectors_db()
    with connect() as con:
        cur = con.execute("DELETE FROM mcp_connectors WHERE id=?", (connector_id,))
    return cur.rowcount > 0
