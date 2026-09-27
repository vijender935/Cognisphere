from types import SimpleNamespace

from agent import _tool_choice_for_round


def test_explicit_tool_task_requires_tool_on_first_round():
    plan = SimpleNamespace(needs_web=False, needs_local_tools=False, needs_mcp=True)
    state = SimpleNamespace(tool_calls=0)
    assert _tool_choice_for_round(plan, state, [{"type": "function"}]) == "required"


def test_tool_task_returns_to_auto_after_a_tool_call():
    plan = SimpleNamespace(needs_web=False, needs_local_tools=False, needs_mcp=True)
    state = SimpleNamespace(tool_calls=1)
    assert _tool_choice_for_round(plan, state, [{"type": "function"}]) == "auto"


def test_no_tools_means_no_tool_choice():
    plan = SimpleNamespace(needs_web=True, needs_local_tools=False, needs_mcp=False)
    state = SimpleNamespace(tool_calls=0)
    assert _tool_choice_for_round(plan, state, []) is None
