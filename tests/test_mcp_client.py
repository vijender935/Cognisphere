import mcp_client
def test_mcp_discovery_opt_in(monkeypatch):
 monkeypatch.delenv("MCP_SERVERS",raising=False);mcp_client._DISCOVERY_CACHE.update(key=None,expires_at=0.0,schemas=[]);assert mcp_client.discover_tool_schemas()==[]
def test_tool_name_validation():
 import pytest
 with pytest.raises(ValueError):mcp_client.call_tool("calculator",{})
def test_timeout_bounded(monkeypatch):
 monkeypatch.setenv("MCP_TIMEOUT_SECONDS","999");assert mcp_client._timeout_seconds()==120.0
 monkeypatch.setenv("MCP_TIMEOUT_SECONDS","0");assert mcp_client._timeout_seconds()==1.0
def test_permissions(monkeypatch):
 monkeypatch.setenv("MCP_SERVERS",'{"github":{"transport":"streamable-http","url":"https://example/mcp","allowed_tools":["list_issues"]}}')
 from mcp_registry import load_server_configs,tool_allowed
 c=load_server_configs()[0];assert c.name=="github" and tool_allowed(c,"list_issues") and not tool_allowed(c,"delete_repository")
def test_connector_header_persistence(monkeypatch):
 import connectors
 monkeypatch.delenv("ALLOW_LOCAL_MCP",raising=False)
 name="github-test"
 try:
  c=connectors.upsert_connector(name,"streamable-http","https://example.com/mcp",headers={"Authorization":"Bearer secret"})
  assert c["headers"]["Authorization"]=="Bearer secret"
  updated=connectors.upsert_connector(name,"streamable-http","https://example.com/mcp",headers={"Authorization":"Bearer new"})
  assert updated["headers"]=={"Authorization":"Bearer new"}
 finally:
  rows=[x for x in connectors.list_connectors() if x["name"]==name]
  if rows: connectors.delete_connector(rows[0]["id"])

def test_orchestrator_uses_stable_server_tool_names():
 import mcp_client
 from types import SimpleNamespace
 token=mcp_client._CURRENT_SERVER_NAME.set("github-main")
 try:
  assert mcp_client._component_name("list_issues",SimpleNamespace(name="GitHub"))=="github-main__list_issues"
 finally:
  mcp_client._CURRENT_SERVER_NAME.reset(token)


def test_connector_status_persists(monkeypatch):
 import connectors
 name="status-test"
 try:
  c=connectors.upsert_connector(name,"streamable-http","https://example.com/mcp")
  updated=connectors.update_connector_status(c["id"],{"connected":True,"tools":2,"tool_names":["mcp__status-test__one","mcp__status-test__two"]})
  assert updated["status"]["connected"] is True
  assert updated["status"]["tools"]==2
  listed=next(x for x in connectors.list_connectors() if x["id"]==c["id"])
  assert listed["status"]["tool_names"][-1].endswith("__two")
 finally:
  rows=[x for x in connectors.list_connectors() if x["name"]==name]
  if rows: connectors.delete_connector(rows[0]["id"])

def test_component_name_alias_keeps_tools_for_connector_names_with_spaces():
 from types import SimpleNamespace
 config=SimpleNamespace(name="Search Image")
 indexed=mcp_client._configs_by_component_name([config])
 assert indexed["Search Image"] is config
 assert indexed["Search_Image"] is config


def test_static_oauth_client_config_is_connector_agnostic(monkeypatch):
 import mcp_oauth
 monkeypatch.setenv(
  "MCP_OAUTH_CLIENTS_JSON",
  '{"Github":{"client_id":"client-123","client_secret":"secret-456","token_endpoint_auth_method":"client_secret_post"}}',
 )
 config=mcp_oauth._static_client_config({"id":42,"name":"Github"})
 assert config["client_id"]=="client-123"
 assert config["client_secret"]=="secret-456"
 assert config["token_endpoint_auth_method"]=="client_secret_post"


def test_static_oauth_client_info_skips_dynamic_registration(monkeypatch):
 import asyncio
 import mcp_oauth
 monkeypatch.setenv(
  "MCP_OAUTH_CLIENTS_JSON",
  '{"Github":{"client_id":"client-123","client_secret":"secret-456"}}',
 )

 class Storage:
  def __init__(self):
   self.info=None
  async def get_client_info(self):
   return self.info
  async def set_client_info(self, info):
   self.info=info

 storage=Storage()
 asyncio.run(
  mcp_oauth._prime_static_client_info(
   storage,
   {"id":42,"name":"Github"},
   "https://personal-ai-assistant-api-dwp6.onrender.com/api/v1/mcp/oauth/callback",
  )
 )
 assert storage.info.client_id=="client-123"
 assert storage.info.client_secret=="secret-456"
 assert storage.info.token_endpoint_auth_method=="client_secret_post"
