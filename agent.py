"""Personal AI Agent — conversational + persistent memory + local tools."""
from __future__ import annotations
import json, logging, os, sys, time
from groq import Groq
from config import MAX_HISTORY_MESSAGES, MAX_ITERATIONS, MAX_RETRIES, MODEL, VISION_MODEL
from orchestration import (
    ExecutionState, build_execution_plan, plan_prompt, plan_task,
    recovery_instruction, select_relevant_tools, should_continue_execution,
    validate_tool_result,
)
from tools import TOOL_FUNCTIONS, TOOL_SCHEMAS, init_db, load_history, semantic_recall_memories, remember_fact, save_turn

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

SYSTEM_PROMPT = """Tum ek helpful personal AI assistant ho.
User se natural Hinglish me baat karo.
Context ko yaad rakho aur previous conversation ko use karo.
Jab current information, calculation, file operation ya shell task ki zaroorat ho, appropriate tool use karo.
Tool results ko clearly explain karo. Kabhi bhi tool result invent mat karo.
Dangerous/destructive local actions se bacho."""

def _extract_memory_candidate(text):
    lower = text.lower().strip()
    prefixes = ("remember that ", "yaad rakhna ", "yaad rakho ", "note that ", "remember: ", "yaad rakho:")
    for prefix in prefixes:
        if lower.startswith(prefix):
            return text.strip()[len(prefix):].strip()
    return None

def build_messages(goal, session_id, rag_sources=None, memory_enabled=True, tool_budget=False):
    # Groq applies an input-token budget independently of the model's large
    # context window. Tool definitions are part of the input, so tool calls
    # need a smaller conversation slice than ordinary chat.
    default_context = int(os.getenv("MAX_CONTEXT_CHARS", "24000"))
    tool_context = int(os.getenv("MAX_TOOL_CONTEXT_CHARS", "12000"))
    max_context_chars = max(8000, tool_context if tool_budget else default_context)
    max_history = min(MAX_HISTORY_MESSAGES, max(4, int(os.getenv("MAX_CONTEXT_HISTORY", "12"))))
    history = load_history(session_id, max_history)
    plan = plan_task(goal)

    try:
        from preferences import get_preferences
        preferences = get_preferences()
    except Exception:
        preferences = {}
    system_prompt = SYSTEM_PROMPT
    custom_instructions = str(preferences.get("custom_instructions", "") or "").strip()
    response_style = preferences.get("response_style", "Natural")
    if custom_instructions:
        system_prompt += "\n\nUser customization (follow when it does not conflict with safety or the current task):\n" + custom_instructions
    if response_style == "Concise":
        system_prompt += "\nPrefer concise answers unless the user asks for detail."
    elif response_style == "Detailed":
        system_prompt += "\nPrefer thorough, structured answers when useful."
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": plan_prompt(plan)},
    ]
    used_chars = sum(len(str(m["content"])) for m in messages) + len(goal)

    # Avoid vector/RAG lookups for ordinary conversation; they add latency and
    # are only useful when the request actually asks for memory/document context.
    memories = semantic_recall_memories(goal, limit=8) if (memory_enabled and plan.needs_memory) else []
    if memories and used_chars < max_context_chars:
        memory_text = "Relevant saved memories (semantic retrieval):\n" + "\n".join(
            f"- {m}" for m in memories
        )
        messages.append({
            "role": "system",
            "content": memory_text[:min(4000, max_context_chars - used_chars)],
        })
        used_chars += len(messages[-1]["content"])

    try:
        from memory import search_rag
        rag_results = search_rag(goal, limit=4, sources=rag_sources) if (rag_sources or plan.needs_rag) else []
    except Exception:
        rag_results = []
    if rag_results and used_chars < max_context_chars:
        context = "\n\n".join(
            f"[{item['source']} | score={item['score']}]\n{item['content']}" for item in rag_results
        )
        remaining = max_context_chars - used_chars
        rag_text = ("Relevant knowledge-base context:\n" + context)[:min(8000, remaining)]
        messages.append({"role": "system", "content": rag_text})
        used_chars += len(rag_text)

    # Add the newest conversation turns first, walking backwards until the
    # bounded context budget is reached. Never send an oversized historical
    # answer just because it is a single database row.
    remaining = max_context_chars - used_chars - len(goal)
    selected = []
    for item in reversed(history):
        content = str(item.get("content", ""))
        if not content:
            continue
        content = content[:6000]
        cost = len(content)
        if selected and cost > remaining:
            continue
        if cost > remaining:
            content = content[-remaining:] if remaining > 0 else ""
            cost = len(content)
        if not content:
            break
        selected.append({"role": item["role"], "content": content})
        remaining -= cost
        if remaining <= 0:
            break

    messages.extend(reversed(selected))
    messages.append({"role": "user", "content": goal})
    return messages

def _tool_schemas_for(goal, web_search_enabled=True):
    """Build a small tool catalog using live MCP metadata.

    MCP discovery is independent of static intent keywords. Every non-empty
    request is compared against the currently advertised MCP tools, so adding
    or removing a server/tool does not require code changes.
    """
    goal = goal.strip()
    if not goal:
        return []

    plan = plan_task(goal)
    candidates = []

    if plan.needs_web and web_search_enabled:
        candidates.extend(
            schema for schema in TOOL_SCHEMAS
            if schema.get("function", {}).get("name") == "web_search"
        )

    local_candidates = []
    if plan.needs_local_tools:
        local_candidates = [
            schema for schema in TOOL_SCHEMAS
            if schema.get("function", {}).get("name")
            in {"calculator", "read_file", "write_file", "run_shell"}
        ]

    mcp_candidates = []
    try:
        from mcp_client import discover_tool_schemas
        mcp_candidates = discover_tool_schemas()
    except Exception as exc:
        logger.warning("MCP discovery unavailable: %s", exc)

    # Route both local and MCP candidates through the same metadata-driven
    # selector. Groq recommends keeping the active tool set small; the selector
    # stays dynamic and metadata-driven rather than hardcoding server/tool names.
    routed_candidates = select_relevant_tools(
        local_candidates + mcp_candidates,
        goal,
        max_tools=4,
    )
    candidates.extend(routed_candidates)

    unique = {}
    for schema in candidates:
        name = schema.get("function", {}).get("name")
        if name:
            unique[name] = schema
    return list(unique.values())

def _compact_tool_schema(schema):
    """Reduce tool-schema tokens without changing the callable contract."""
    import copy

    compact = copy.deepcopy(schema)
    fn = compact.get("function", {})
    if isinstance(fn, dict):
        if isinstance(fn.get("description"), str):
            fn["description"] = fn["description"][:800]
        params = fn.get("parameters")
        if isinstance(params, dict):
            def compact_node(node):
                if not isinstance(node, dict):
                    return node
                keep = {}
                for key in ("type", "enum", "required", "properties", "items", "additionalProperties", "anyOf", "oneOf"):
                    if key in node:
                        keep[key] = node[key]
                if isinstance(keep.get("properties"), dict):
                    keep["properties"] = {
                        str(name): compact_node(value)
                        for name, value in keep["properties"].items()
                        if isinstance(value, dict)
                    }
                if isinstance(keep.get("items"), dict):
                    keep["items"] = compact_node(keep["items"])
                for key in ("anyOf", "oneOf"):
                    if isinstance(keep.get(key), list):
                        keep[key] = [compact_node(item) for item in keep[key] if isinstance(item, dict)]
                return keep
            fn["parameters"] = compact_node(params)
    return compact


def _prepare_tool_schemas(schemas):
    return [_compact_tool_schema(schema) for schema in schemas[:4]]


def _tool_choice_for_turn(tool_schemas, task_plan, state):
    """Choose Groq tool mode for the current execution turn.

    On the first turn of an explicit tool-bearing task, require at least one
    tool call. Once a tool has executed, return to auto so the model can either
    call another relevant tool or compose the final answer. Ordinary chat stays
    unchanged even when the dynamic catalog happens to contain tools.
    """
    if not tool_schemas:
        return None
    if state.tool_calls == 0 and (
        task_plan.needs_web
        or task_plan.needs_local_tools
        or task_plan.needs_mcp
        or task_plan.needs_rag
    ):
        return "required"
    return "auto"


def _execute_tool(name, args):
    try:
        fn = TOOL_FUNCTIONS.get(name)
        if fn:
            return fn(**args)
        if name.startswith("mcp__"):
            from mcp_client import call_tool
            return call_tool(name, args)
        return f"Unknown tool: {name}"
    except Exception as exc:
        logger.exception("Tool failed: %s", name)
        return f"Tool error in {name}: {exc}"

def _expand_tools_after_failure(goal, current_schemas, failure_text):
    """Add recovery tools from the cached MCP catalog after a tool failure."""
    try:
        from mcp_client import discover_tool_schemas
        catalog = discover_tool_schemas()
    except Exception as exc:
        logger.warning("MCP recovery discovery unavailable: %s", exc)
        return current_schemas

    recovery_goal = (
        f"{goal}\nTool failure: {failure_text}\n"
        "Recover by finding the correct resource or arguments before retrying."
    )
    expanded = select_relevant_tools(catalog, recovery_goal, max_tools=8)
    merged = list(current_schemas)
    existing = {str(s.get("function", {}).get("name", "")) for s in merged}
    for schema in expanded:
        name = str(schema.get("function", {}).get("name", ""))
        if name and name not in existing:
            merged.append(schema)
            existing.add(name)
    return _prepare_tool_schemas(merged)

def _prepare_goal(goal):
    explicit_memory = _extract_memory_candidate(goal)
    if explicit_memory:
        remember_fact(explicit_memory, source="user-explicit")
        try:
            from memory import remember_semantic
            remember_semantic(explicit_memory, source="user-explicit")
        except Exception as exc:
            logger.warning("Semantic memory unavailable: %s", exc)

def _is_context_length_error(exc):
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    return status == 400 and ("reduce the length" in text or "messages or completion" in text or "context length" in text or "maximum context" in text)


def _runtime_context_limit():
    try:
        value = int(os.getenv("MAX_RUNTIME_CONTEXT_CHARS", "14000"))
    except ValueError:
        value = 14000
    return max(8000, min(value, 60000))


def _compact_runtime_messages(messages):
    """Bound live agent context after tool results are appended."""
    limit = _runtime_context_limit()
    try:
        tool_limit = max(1000, int(os.getenv("MAX_TOOL_RESULT_CHARS", "1800")))
    except ValueError:
        tool_limit = 3500
    normalized = []
    for message in messages:
        item = dict(message)
        if item.get("role") == "tool":
            content = str(item.get("content", ""))
            if len(content) > tool_limit:
                item["content"] = content[:tool_limit] + "\n[Tool result truncated.]"
        normalized.append(item)

    systems = [m for m in normalized if m.get("role") == "system"]
    user_indexes = [i for i, m in enumerate(normalized) if m.get("role") == "user"]
    last_user = user_indexes[-1] if user_indexes else -1
    current_user = normalized[last_user] if last_user >= 0 else None

    rounds = []
    current = []
    for message in normalized[last_user + 1:] if last_user >= 0 else []:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            if current:
                rounds.append(current)
            current = [message]
        elif current and message.get("role") == "tool":
            current.append(message)
        elif current:
            rounds.append(current)
            current = []
    if current:
        rounds.append(current)

    tail = []
    for exchange in rounds[-2:]:
        tail.extend(exchange)
    if not tail and last_user > 0:
        prior = [m for m in normalized[:last_user] if m.get("role") in {"user", "assistant"}]
        tail.extend(prior[-4:])
    if current_user is not None:
        tail.append(current_user)

    result = systems + tail
    seen = set()
    result = [m for m in result if not (id(m) in seen or seen.add(id(m)))]
    def total_size(items):
        return sum(len(str(m.get("content", ""))) for m in items)
    while total_size(result) > limit:
        index = next((i for i, m in enumerate(result) if m.get("role") in {"user", "assistant"} and not m.get("tool_calls") and m is not current_user), None)
        if index is None:
            break
        result.pop(index)
    return result


def _completion_budget(has_tools):
    try:
        value = int(os.getenv("MAX_COMPLETION_TOKENS", "1024" if has_tools else "2048"))
    except ValueError:
        value = 1024 if has_tools else 2048
    return max(256, min(value, 4096))

def run_agent(goal, session_id="default", image_urls=None, rag_sources=None, verbose=True, memory_enabled=True, web_search_enabled=True):
    goal = goal.strip()
    if not goal:
        return "Please enter a message."
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return "❌ GROQ_API_KEY set nahi hai. README.md dekho setup ke liye."

    _prepare_goal(goal)
    task_plan = plan_task(goal)
    execution_plan = build_execution_plan(task_plan)
    client = Groq(api_key=api_key)
    tool_schemas = _prepare_tool_schemas(
        _tool_schemas_for(goal, web_search_enabled=web_search_enabled)
    )
    messages = build_messages(
        goal,
        session_id,
        rag_sources=rag_sources,
        memory_enabled=memory_enabled,
        tool_budget=bool(tool_schemas),
    )
    # A dynamically discovered MCP tool makes an otherwise "simple" task
    # tool-bearing. Give the execution loop enough room for tool -> result ->
    # final-answer, without changing the static planner.
    tool_round_limit = min(
        MAX_ITERATIONS,
        max(execution_plan.max_tool_rounds, 4 if tool_schemas else 1),
    )

    if image_urls:
        messages[-1]["content"] = [{"type": "text", "text": goal}] + [
            {"type": "image_url", "image_url": {"url": url}} for url in image_urls
        ]

    state = ExecutionState()
    failed_call_signatures = set()
    while should_continue_execution(state, tool_round_limit):
        state.round_number += 1
        response = None
        messages = _compact_runtime_messages(messages)
        for retry in range(MAX_RETRIES + 1):
            try:
                request_kwargs = {
                    "model": VISION_MODEL if image_urls else MODEL,
                    "messages": messages,
                    "temperature": 0.4,
                    "max_completion_tokens": _completion_budget(bool(tool_schemas)),
                }
                if tool_schemas:
                    request_kwargs.update({
                        "tools": tool_schemas,
                        "tool_choice": _tool_choice_for_turn(tool_schemas, task_plan, state),
                    })
                response = client.chat.completions.create(**request_kwargs)
                break
            except Exception as exc:
                if _is_context_length_error(exc):
                    messages = _compact_runtime_messages(messages)
                    logger.warning("Groq context limit hit; compacted messages (round=%s, chars=%s)",
                                   state.round_number,
                                   sum(len(str(m.get("content", ""))) for m in messages))
                    if retry < MAX_RETRIES:
                        continue
                logger.warning("Model request failed (retry %s/%s, tools=%s): %s",
                               retry, MAX_RETRIES,
                               [s.get("function", {}).get("name") for s in tool_schemas], exc)
                if retry < MAX_RETRIES:
                    time.sleep(2 ** retry)
        if response is None:
            return "❌ Model/API request failed after retries. Context/tool request could not be accepted; server logs me exact error recorded hai."

        msg = response.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            answer = msg.content or ""
            save_turn(session_id, goal, answer)
            return answer

        for call in msg.tool_calls:
            name = call.function.name
            state.tool_calls += 1
            state.last_tool = name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if verbose:
                logger.info("Tool call: %s(%s)", name, args)

            call_signature = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
            if call_signature in failed_call_signatures:
                result = (
                    f"Tool error in {name}: the identical tool call already failed. "
                    "Do not repeat it; use a search/discovery tool or change the arguments."
                )
            else:
                result = _execute_tool(name, args)
            validated = validate_tool_result(result)
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "name": name,
                "content": validated.content,
            })
            if validated.ok:
                state.consecutive_failures = 0
            else:
                state.consecutive_failures += 1
                failed_call_signatures.add(call_signature)
                tool_schemas = _expand_tools_after_failure(
                    goal, tool_schemas, validated.content
                )
                messages.append({
                    "role": "system",
                    "content": recovery_instruction(name, validated),
                })

    if state.consecutive_failures >= 2:
        return "⚠️ Tool execution repeatedly failed. Maine unsafe/infinite retry se bachne ke liye execution stop kar diya."
    return f"⚠️ Max tool iterations ({MAX_ITERATIONS}) reached — task incomplete reh gaya."

def stream_agent(goal, session_id="default", image_urls=None, rag_sources=None, memory_enabled=True, web_search_enabled=True):
    """Execute tools first when required, then stream the final assistant response."""
    goal = goal.strip()
    if not goal:
        yield "Please enter a message."
        return
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        yield "❌ GROQ_API_KEY set nahi hai."
        return

    _prepare_goal(goal)
    task_plan = plan_task(goal)
    execution_plan = build_execution_plan(task_plan)
    client = Groq(api_key=api_key)
    tool_schemas = _prepare_tool_schemas(
        _tool_schemas_for(goal, web_search_enabled=web_search_enabled)
    )
    messages = build_messages(
        goal,
        session_id,
        rag_sources=rag_sources,
        memory_enabled=memory_enabled,
        tool_budget=bool(tool_schemas),
    )
    tool_round_limit = min(
        MAX_ITERATIONS,
        max(execution_plan.max_tool_rounds, 4 if tool_schemas else 1),
    )

    if image_urls:
        messages[-1]["content"] = [{"type": "text", "text": goal}] + [
            {"type": "image_url", "image_url": {"url": url}} for url in image_urls
        ]

    state = ExecutionState()
    failed_call_signatures = set()
    while should_continue_execution(state, tool_round_limit):
        state.round_number += 1
        response = None
        messages = _compact_runtime_messages(messages)

        for retry in range(MAX_RETRIES + 1):
            try:
                request_kwargs = {
                    "model": VISION_MODEL if image_urls else MODEL,
                    "messages": messages,
                    "temperature": 0.4,
                    "max_completion_tokens": _completion_budget(bool(tool_schemas)),
                }
                if tool_schemas:
                    request_kwargs.update({
                        "tools": tool_schemas,
                        "tool_choice": _tool_choice_for_turn(tool_schemas, task_plan, state),
                    })
                response = client.chat.completions.create(**request_kwargs)
                break
            except Exception as exc:
                if _is_context_length_error(exc):
                    messages = _compact_runtime_messages(messages)
                    logger.warning("Groq context limit hit during streaming; compacted messages (round=%s, chars=%s)",
                                   state.round_number,
                                   sum(len(str(m.get("content", ""))) for m in messages))
                    if retry < MAX_RETRIES:
                        continue
                logger.warning("Streaming preparation request failed (retry %s/%s, tools=%s): %s",
                               retry, MAX_RETRIES,
                               [s.get("function", {}).get("name") for s in tool_schemas], exc)
                if retry < MAX_RETRIES:
                    time.sleep(2 ** retry)

        if response is None:
            yield "❌ Model/API request failed after retries. Context/tool request could not be accepted; server logs me exact error recorded hai."
            return

        msg = response.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            answer = msg.content or ""
            if answer:
                words = answer.split(" ")
                for i, word in enumerate(words):
                    yield word + (" " if i < len(words) - 1 else "")
            save_turn(session_id, goal, answer)
            return

        for call in msg.tool_calls:
            name = call.function.name
            state.tool_calls += 1
            state.last_tool = name
            yield "🔧 Calling tool: " + name + "...\n\n"

            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            call_signature = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
            if call_signature in failed_call_signatures:
                result = (
                    f"Tool error in {name}: the identical tool call already failed. "
                    "Do not repeat it; use a search/discovery tool or change the arguments."
                )
            else:
                result = _execute_tool(name, args)
            validated = validate_tool_result(result)
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "name": name,
                "content": validated.content,
            })

            if validated.ok:
                state.consecutive_failures = 0
            else:
                state.consecutive_failures += 1
                failed_call_signatures.add(call_signature)
                tool_schemas = _expand_tools_after_failure(
                    goal, tool_schemas, validated.content
                )
                messages.append({
                    "role": "system",
                    "content": recovery_instruction(name, validated),
                })

        if state.consecutive_failures >= 2:
            yield "⚠️ Tool execution repeatedly failed."
            return

        if state.round_number >= tool_round_limit:
            yield f"⚠️ Max tool iterations ({tool_round_limit}) reached — task incomplete reh gaya."
            return

def interactive():
    session_id = os.getenv("AGENT_SESSION", "default")
    print(f"Personal AI Agent — model: {MODEL}")
    print("Commands: /new, /remember <fact>, /memories, /exit\n")
    while True:
        try:
            goal = input("Tum: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not goal:
            continue
        command = goal.lower()
        if command in {"/exit", "/quit", "exit", "quit"}:
            break
        if command == "/new":
            session_id = f"session-{time.time_ns()}"
            print(f"🆕 New session: {session_id}\n")
            continue
        if command == "/memories":
            print("\n".join(f"- {m}" for m in semantic_recall_memories("", limit=50)) or "(no memories)")
            print()
            continue
        if command.startswith("/remember "):
            remember_fact(goal[len("/remember "):].strip(), source="user-command")
            print("🧠 Memory saved.\n")
            continue
        print("\nAgent:", run_agent(goal, session_id=session_id), "\n")

if __name__ == "__main__":
    init_db()
    print(run_agent(" ".join(sys.argv[1:]))) if len(sys.argv) > 1 else interactive()
