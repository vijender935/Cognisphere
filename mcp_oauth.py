"""Persistent OAuth storage and flow handling for single-user MCP connectors."""
from __future__ import annotations
import asyncio,json,os,secrets,time
from dataclasses import dataclass
from urllib.parse import parse_qs,urlparse
from config import ensure_directories
from db import connect
def oauth_redirect_uri():
    base=os.getenv("PUBLIC_BASE_URL","").strip().rstrip("/")
    if not base: base=os.getenv("API_BASE_URL","").strip().rstrip("/")
    if not base: base="http://127.0.0.1:8000"
    return base+"/api/v1/mcp/oauth/callback"
def init_oauth_db():
    ensure_directories()
    with connect() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS mcp_oauth_credentials(
            connector_id BIGINT PRIMARY KEY,tokens TEXT,client_info TEXT,updated_at DOUBLE PRECISION NOT NULL)""")
        try:
            con.execute("ALTER TABLE mcp_oauth_credentials DROP COLUMN IF EXISTS user_id")
        except Exception:
            pass

def _connector(connector_id):
    from connectors import list_connectors
    connector=next((x for x in list_connectors() if x["id"]==connector_id),None)
    if not connector:raise ValueError("Connector not found.")
    if connector["transport"]!="streamable-http":raise ValueError("OAuth currently requires a Streamable HTTP connector.")
    return connector

try:
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata,OAuthToken,OAuthClientInformationFull,AuthorizationCodeResult
    from pydantic import AnyUrl
except Exception:
    OAuthClientProvider=OAuthClientMetadata=OAuthToken=OAuthClientInformationFull=AuthorizationCodeResult=None

class DatabaseOAuthStorage:
    def __init__(self,connector_id): self.connector_id=connector_id; init_oauth_db()
    def _read(self):
        with connect() as con: row=con.execute("SELECT tokens,client_info FROM mcp_oauth_credentials WHERE connector_id=?",(self.connector_id,)).fetchone()
        tokens= json.loads(row[0]) if row and row[0] else None
        client= json.loads(row[1]) if row and row[1] else None
        return tokens,client
    def _write(self,tokens,client):
        with connect() as con:
            values=(self.connector_id,json.dumps(tokens) if tokens else None,json.dumps(client) if client else None,time.time())
            con.execute("""INSERT INTO mcp_oauth_credentials(connector_id,tokens,client_info,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(connector_id) DO UPDATE SET tokens=excluded.tokens,client_info=excluded.client_info,updated_at=excluded.updated_at""",values)
    async def get_tokens(self):
        tokens,_=self._read()
        return OAuthToken.model_validate(tokens) if tokens and OAuthToken else None
    async def set_tokens(self,tokens):
        _,client=self._read(); self._write(tokens.model_dump(mode="json"),client)
    async def get_client_info(self):
        _,client=self._read()
        return OAuthClientInformationFull.model_validate(client) if client and OAuthClientInformationFull else None
    async def set_client_info(self,client):
        tokens,_=self._read(); self._write(tokens,client.model_dump(mode="json"))

@dataclass
class _OAuthFlow:
    connector_id:int
    ready:asyncio.Future
    callback:asyncio.Future
    auth_url:str|None=None
    oauth_state:str|None=None
    task:asyncio.Task|None=None
    created_at:float=0.0
_FLOWS={}; _STATE_TO_FLOW={}; _FLOW_TTL=600

def _cleanup_flows():
    now=time.time()
    for flow_id,flow in list(_FLOWS.items()):
        if now-flow.created_at>_FLOW_TTL:
            if not flow.ready.done():flow.ready.cancel()
            if not flow.callback.done():flow.callback.cancel()
            if flow.task and not flow.task.done():flow.task.cancel()
            _FLOWS.pop(flow_id,None)

async def _run_flow(flow_id,flow,redirect_uri):
    connector=_connector(flow.connector_id); storage=DatabaseOAuthStorage(flow.connector_id)
    async def redirect_handler(url):
        flow.auth_url=url
        try:
            flow.oauth_state=parse_qs(urlparse(url).query).get("state",[None])[0]
            if flow.oauth_state:_STATE_TO_FLOW[flow.oauth_state]=flow_id
        except Exception:flow.oauth_state=None
        if not flow.ready.done():flow.ready.set_result(url)
    async def callback_handler():return await flow.callback
    provider=OAuthClientProvider(server_url=connector["url"],client_metadata=OAuthClientMetadata(client_name="Personal AI Assistant",redirect_uris=[AnyUrl(redirect_uri)],application_type="web"),storage=storage,redirect_handler=redirect_handler,callback_handler=callback_handler)
    import httpx2
    from mcp.client.streamable_http import streamable_http_client
    from mcp import Client
    async with httpx2.AsyncClient(
        auth=provider,
        timeout=httpx2.Timeout(30.0, read=300.0),
    ) as http_client:
        transport = streamable_http_client(
            connector["url"],
            http_client=http_client,
            terminate_on_close=True,
        )
        async with Client(transport) as client:
            await client.list_tools()
    if not flow.ready.done():flow.ready.set_result(flow.auth_url or "")

async def begin_oauth(connector_id,redirect_uri):
    _cleanup_flows(); _connector(connector_id); flow_id=secrets.token_urlsafe(32); loop=asyncio.get_running_loop()
    flow=_OAuthFlow(connector_id=connector_id,ready=loop.create_future(),callback=loop.create_future(),created_at=time.time())
    _FLOWS[flow_id]=flow; flow.task=asyncio.create_task(_run_flow(flow_id,flow,redirect_uri))
    try:auth_url=await asyncio.wait_for(asyncio.shield(flow.ready),timeout=15)
    except Exception:
        _FLOWS.pop(flow_id,None)
        if flow.task and not flow.task.done():flow.task.cancel()
        raise
    return {"flow_id":flow_id,"authorization_url":auth_url}

async def complete_oauth(flow_id,code,state=None,iss=None):
    _cleanup_flows(); flow=_FLOWS.get(flow_id)
    if not flow and state:
        flow_id=_STATE_TO_FLOW.get(state); flow=_FLOWS.get(flow_id) if flow_id else None
    if not flow:raise ValueError("OAuth flow expired or was not found.")
    if flow.callback.done():raise ValueError("OAuth callback was already submitted.")
    flow.callback.set_result(AuthorizationCodeResult(code=code,state=state,iss=iss))
    try:await asyncio.wait_for(asyncio.shield(flow.task),timeout=120)
    except asyncio.TimeoutError as exc:raise RuntimeError("OAuth token exchange timed out.") from exc
    finally:
        _FLOWS.pop(flow_id,None)
        if flow.oauth_state:_STATE_TO_FLOW.pop(flow.oauth_state,None)
    return {"connected":True,"connector_id":flow.connector_id}

def oauth_token_present(connector_id):
    _connector(connector_id); init_oauth_db()
    with connect() as con:row=con.execute("SELECT tokens FROM mcp_oauth_credentials WHERE connector_id=?",(connector_id,)).fetchone()
    if not row or not row[0]:return False
    try:return bool(json.loads(row[0]).get("access_token"))
    except (TypeError,json.JSONDecodeError):return False

def oauth_access_token(connector_id):
    """Return the persisted OAuth access token for MCP HTTP requests."""
    _connector(connector_id)
    init_oauth_db()
    with connect() as con:
        row = con.execute(
            "SELECT tokens FROM mcp_oauth_credentials WHERE connector_id=?",
            (connector_id,),
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        tokens = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return None
    token = tokens.get("access_token") if isinstance(tokens, dict) else None
    return token if isinstance(token, str) and token.strip() else None


def oauth_status(connector_id):
    _connector(connector_id); storage=DatabaseOAuthStorage(connector_id)
    async def read():return await storage.get_tokens()
    try:token=asyncio.run(read())
    except RuntimeError:token=None
    return {"connected":bool(token and token.access_token)}

def clear_oauth(connector_id):
    _connector(connector_id); init_oauth_db()
    with connect() as con:cur=con.execute("DELETE FROM mcp_oauth_credentials WHERE connector_id=?",(connector_id,))
    return cur.rowcount>0
