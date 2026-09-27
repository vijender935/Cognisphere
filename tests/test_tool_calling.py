from agent import _tool_choice_for_turn
from orchestration import ExecutionState, plan_task


def test_explicit_mcp_task_requires_first_tool_call():
    state = ExecutionState()
    task_plan = plan_task("GitHub repo ki latest issues dikhao")
    schemas = [{
        "type": "function",
        "function": {
            "name": "mcp__Github__list_recent_issues",
            "description": "List recent GitHub issues",
            "parameters": {"type": "object", "properties": {}},
        },
    }]

    assert _tool_choice_for_turn(schemas, task_plan, state) == "required"


def test_after_tool_execution_model_returns_to_auto():
    state = ExecutionState(tool_calls=1)
    task_plan = plan_task("GitHub repo ki latest issues dikhao")
    schemas = [{
        "type": "function",
        "function": {
            "name": "mcp__Github__list_recent_issues",
            "description": "List recent GitHub issues",
            "parameters": {"type": "object", "properties": {}},
        },
    }]

    assert _tool_choice_for_turn(schemas, task_plan, state) == "auto"


def test_normal_conversation_does_not_force_tools():
    state = ExecutionState()
    task_plan = plan_task("Python me list kya hoti hai?")
    schemas = [{
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "Calculate arithmetic expressions",
            "parameters": {"type": "object", "properties": {}},
        },
    }]

    assert _tool_choice_for_turn(schemas, task_plan, state) == "auto"
