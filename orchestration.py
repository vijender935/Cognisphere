"""Deterministic planning helpers for Personal AI Agent orchestration."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionPlan:
    steps: tuple[str, ...]
    max_tool_rounds: int


@dataclass
class ExecutionState:
    round_number: int = 0
    tool_calls: int = 0
    consecutive_failures: int = 0
    last_tool: str = ""


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: str
    recoverable: bool = False


@dataclass(frozen=True)
class TaskPlan:
    intent: str
    needs_memory: bool
    needs_rag: bool
    needs_web: bool
    needs_local_tools: bool
    needs_mcp: bool
    complexity: str


_WEB_TERMS = {
    "latest", "today", "current", "news", "weather", "price", "search",
    "online", "internet", "recent", "abhi", "aaj", "taaza",
}
_RAG_TERMS = {
    "document", "pdf", "file", "uploaded", "according to", "according",
    "document me", "file me", "meri file", "mere document",
}
_LOCAL_TERMS = {
    "calculate", "calculator", "math", "file", "read", "write", "folder",
    "directory", "shell", "terminal", "run command", "rename",
}
_MCP_TERMS = {
    "github", "slack", "notion", "telegram", "mcp", "repository", "repo",
    "pull request", "issue", "database", "db", "sql", "table", "record",
    "records", "query", "queries",
}
_MEMORY_TERMS = {
    "remember", "memory", "yaad", "previous", "pehle", "last time",
    "meri preference", "what did i tell you",
}


def _contains(text: str, terms: set[str]) -> bool:
    return any(term in text for term in terms)


def plan_task(goal: str) -> TaskPlan:
    text = goal.lower().strip()
    explicit_memory = text.startswith((
        "remember ", "remember that ", "yaad rakho", "yaad rakhna", "note that ",
    ))
    needs_memory = explicit_memory or _contains(text, _MEMORY_TERMS)
    needs_rag = _contains(text, _RAG_TERMS)
    needs_web = _contains(text, _WEB_TERMS)
    needs_local = _contains(text, _LOCAL_TERMS)
    needs_mcp = _contains(text, _MCP_TERMS)

    if needs_mcp:
        intent = "external_tool"
    elif needs_rag:
        intent = "document_qa"
    elif needs_web:
        intent = "current_information"
    elif needs_local:
        intent = "local_tool"
    elif needs_memory:
        intent = "memory_context"
    else:
        intent = "conversation"

    signal_count = sum((needs_memory, needs_rag, needs_web, needs_local, needs_mcp))
    complexity = "complex" if signal_count >= 2 else "tool" if signal_count == 1 else "simple"

    return TaskPlan(
        intent=intent,
        needs_memory=needs_memory,
        needs_rag=needs_rag,
        needs_web=needs_web,
        needs_local_tools=needs_local,
        needs_mcp=needs_mcp,
        complexity=complexity,
    )


def validate_tool_result(result: object) -> ToolResult:
    if result is None:
        return ToolResult(False, "Tool returned no result.", recoverable=True)
    content = str(result).strip()
    if not content:
        return ToolResult(False, "Tool returned an empty result.", recoverable=True)
    lowered = content.lower()
    if lowered.startswith(("tool error", "mcp tool error", "unknown tool", "error:")):
        return ToolResult(False, content, recoverable=True)
    # Some remote MCP connectors surface HTTP failures as plain text instead
    # of setting the MCP isError flag. Route common failures through recovery.
    if any(
        marker in lowered
        for marker in (
            "404 not found",
            "status code 404",
            "http 404",
            "401 unauthorized",
            "403 forbidden",
            "429 too many requests",
        )
    ):
        return ToolResult(False, content, recoverable=True)
    return ToolResult(True, content)


def should_continue_execution(state: ExecutionState, max_rounds: int) -> bool:
    return state.round_number < max_rounds and state.consecutive_failures < 2


def recovery_instruction(tool_name: str, result: ToolResult) -> str:
    if result.ok:
        return ""
    return (
        f"Tool '{tool_name}' did not produce a valid result. "
        f"Result: {result.content} "
        "Do not invent missing data. Re-check arguments or choose another available tool; "
        "if recovery is unsafe or impossible, explain the limitation to the user."
    )


def _tokenize(text: str) -> set[str]:
    """Normalize arbitrary tool metadata and user text into comparable terms."""
    import re

    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text or ""))
    return {
        token
        for token in re.findall(r"[a-z0-9]+", expanded.lower())
        if len(token) >= 3
    }


def _tool_metadata(schema: dict) -> tuple[str, str, str]:
    fn = schema.get("function", {}) if isinstance(schema, dict) else {}
    name = str(fn.get("name", ""))

    # Preserve BOTH the MCP server identity and the underlying tool name.
    # Example:
    #   mcp__GitHub__create_issue
    # must match a request containing "GitHub" as well as "issue".
    qualified = name.removeprefix("mcp__")
    if "__" in qualified:
        server_name, tool_name = qualified.split("__", 1)
    else:
        server_name, tool_name = "", qualified

    description = str(fn.get("description", ""))
    parameters = fn.get("parameters") or {}
    properties = parameters.get("properties") if isinstance(parameters, dict) else {}

    metadata_parts = [server_name, tool_name, description]
    if isinstance(properties, dict):
        for key, value in properties.items():
            metadata_parts.append(str(key))
            if isinstance(value, dict):
                metadata_parts.append(str(value.get("title", "")))
                metadata_parts.append(str(value.get("description", "")))
                metadata_parts.extend(
                    str(item) for item in (value.get("enum") or []) if item is not None
                )

    return tool_name, " ".join(metadata_parts), name


def select_relevant_tools(
    schemas: list[dict],
    goal: str,
    max_tools: int = 8,
) -> list[dict]:
    """Select tools from their advertised metadata without server-specific rules.

    Relevance is derived from the current request and each tool's complete
    advertised identity, including MCP server name, tool name, description,
    and input metadata.
    """
    if max_tools < 1:
        return []

    goal_terms = _tokenize(goal)
    if not goal_terms:
        return []

    scored = []
    for schema in schemas:
        tool_name, metadata, display_name = _tool_metadata(schema)
        metadata_terms = _tokenize(metadata)
        name_terms = _tokenize(tool_name)
        description_terms = metadata_terms - name_terms

        exact_name = goal_terms & name_terms
        exact_description = goal_terms & description_terms
        exact_metadata = goal_terms & metadata_terms

        score = (len(exact_name) * 8) + (len(exact_description) * 4)
        score += len(exact_metadata - exact_name - exact_description) * 2

        goal_text = " ".join(sorted(goal_terms))
        metadata_text = " ".join(sorted(metadata_terms))
        if goal_text and goal_text in metadata_text:
            score += 6

        for term in goal_terms:
            if len(term) < 5:
                continue
            for candidate in metadata_terms:
                if candidate == term:
                    continue
                if candidate.startswith(term) or term.startswith(candidate):
                    score += 1
                    break

        scored.append((score, display_name.lower(), schema))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored if item[0] > 0][:max_tools]


# Recovery routing patch: discovery-first after failed remote tool calls.

def select_mcp_tools(
    schemas: list[dict],
    goal: str,
    max_tools: int = 12,
) -> list[dict]:
    """Backward-compatible MCP selector using dynamic tool metadata routing."""
    return select_relevant_tools(schemas, goal, max_tools=max_tools)


def build_execution_plan(plan: TaskPlan) -> ExecutionPlan:
    steps = ["understand_request"]
    if plan.needs_memory:
        steps.append("retrieve_memory")
    if plan.needs_rag:
        steps.append("retrieve_rag")
    if plan.needs_web:
        steps.append("web_or_current_information")
    if plan.needs_local_tools:
        steps.append("local_tool_execution")
    if plan.needs_mcp:
        steps.append("mcp_tool_execution")
    steps.extend(["validate_tool_results", "compose_answer"])

    # Keep remote tool loops bounded. A single model round may contain
    # multiple tool calls, so 4 rounds is enough for normal multi-step work
    # while avoiding runaway MCP/Groq request bursts.
    max_tool_rounds = (
        4 if plan.complexity in {"complex", "tool"}
        else 1
    )
    return ExecutionPlan(steps=tuple(steps), max_tool_rounds=max_tool_rounds)


def plan_prompt(plan: TaskPlan) -> str:
    execution = build_execution_plan(plan)
    routes = []
    if plan.needs_memory:
        routes.append("use relevant saved memory when it helps")
    if plan.needs_rag:
        routes.append("use relevant knowledge-base/document context")
    if plan.needs_web:
        routes.append("use web_search for current or online information")
    if plan.needs_local_tools:
        routes.append("use an appropriate local tool when required")
    if plan.needs_mcp:
        routes.append("use an explicitly configured MCP tool when required")

    route_text = "; ".join(routes) if routes else "answer conversationally without unnecessary tools"
    return (
        f"Task plan: intent={plan.intent}, complexity={plan.complexity}. "
        f"Execution stages: {', '.join(execution.steps)}. "
        f"Max tool rounds: {execution.max_tool_rounds}. "
        f"Routing guidance: {route_text}. "
        "Treat these as hints, not facts; inspect the user's actual request before acting."
    )
