import json
from unittest.mock import MagicMock

import pytest

from mcp_registry import MCPServerConfig
import mcp_client
from agent import run_agent
import tools


def test_end_to_end_agent_mcp_workflow(monkeypatch):
    """
    End-to-End Test proving:
    USER REQUEST -> AGENT -> LLM -> MCP TOOL CALL -> TOOL RESULT -> AGENT ->
    FINAL RESPONSE
    """
    tools.init_db()

    mcp_tool_schema = {
        "type": "function",
        "function": {
            "name": "mcp__github__check_repository",
            "description": "Inspect a GitHub repository for issues and pull requests",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository name"}
                },
                "required": ["repo"],
            },
        },
    }

    monkeypatch.setattr(mcp_client, "discover_tool_schemas", lambda: [mcp_tool_schema])
    tool_executed = {}

    def mock_call_tool(name, arguments):
        tool_executed["name"] = name
        tool_executed["arguments"] = arguments
        return json.dumps({
            "repo": arguments.get("repo"),
            "status": "issues_found",
            "issues": [
                {"id": 101, "title": "Syntax error in mcp_server.py line 60", "state": "open"},
                {"id": 102, "title": "CORS preflight missing Accept header", "state": "open"},
            ],
            "branch": "main"
        })

    monkeypatch.setattr(mcp_client, "call_tool", mock_call_tool)
    mock_groq = MagicMock()

    class FakeCall:
        id = "call_github_check_1"

        class function:
            name = "mcp__github__check_repository"
            arguments = json.dumps({"repo": "Personal-AI-Assistant"})

    msg_turn1 = MagicMock()
    msg_turn1.tool_calls = [FakeCall()]
    msg_turn1.content = None
    msg_turn1.model_dump.return_value = {
        "role": "assistant",
        "tool_calls": [{
            "id": "call_github_check_1",
            "type": "function",
            "function": {
                "name": "mcp__github__check_repository",
                "arguments": json.dumps({"repo": "Personal-AI-Assistant"}),
            },
        }],
    }

    msg_turn2 = MagicMock()
    msg_turn2.tool_calls = None
    msg_turn2.content = (
        "Maine aapka Personal-AI-Assistant GitHub repository check kar liya hai. "
        "Do main issues mile:\n"
        "1. Issue #101: Syntax error in mcp_server.py line 60\n"
        "2. Issue #102: CORS preflight missing Accept header."
    )
    msg_turn2.model_dump.return_value = {
        "role": "assistant",
        "content": msg_turn2.content,
    }

    resp1 = MagicMock()
    resp1.choices = [MagicMock(message=msg_turn1)]
    resp2 = MagicMock()
    resp2.choices = [MagicMock(message=msg_turn2)]
    mock_groq.chat.completions.create.side_effect = [resp1, resp2]
    monkeypatch.setattr("agent.Groq", lambda api_key: mock_groq)

    session_id = "test-e2e-session"
    user_goal = "Check my GitHub repository and tell me what is wrong."

    final_response = run_agent(user_goal, session_id=session_id, verbose=True)

    assert tool_executed.get("name") == "mcp__github__check_repository"
    assert tool_executed.get("arguments") == {"repo": "Personal-AI-Assistant"}
    assert "Issue #101" in final_response
    assert "Issue #102" in final_response
    assert "Syntax error in mcp_server.py" in final_response

    history = tools.load_history(session_id)
    assert len(history) >= 2
    assert history[-2]["role"] == "user"
    assert history[-2]["content"] == user_goal
    assert history[-1]["role"] == "assistant"
    assert history[-1]["content"] == final_response
