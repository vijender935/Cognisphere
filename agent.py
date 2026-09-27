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

def build_messages(goal, session_id, rag_sources=None, memory_enabled=True):
    # Groq's on-demand tier currently enforces a relatively small TPM/request
    # budget. Long conversations can otherwise grow past the limit and produce
    # intermittent 413/token rate-limit failures. Keep the prompt bounded while
    # preserving the newest turns.
    max_context_chars = max(8000, int(os.getenv("MAX_CONTEXT_CHARS", "24000")))
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
    # selector. This prevents a generic local tool from winning simply because
    # a static keyword happened to match the request.
    routed_candidates = select_relevant_tools(
        local_candidates + mcp_candidates,
        goal,
        max_tools=8,
    )
    candidates.extend(routed_candidates)

    unique = {}
    for schema in candidates:
        name = schema.get("function", {}).get("name")
        if name:
            unique[name] = schema
    return list(unique.values())

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

def _prepare_goal(goal):
    explicit_memory = _extract_memory_candidate(goal)
    if explicit_memory:
        remember_fact(explicit_memory, source="user-explicit")
        try:
            from memory import remember_semantic
            remember_semantic(explicit_memory, source="user-explicit")
        except Exception as exc:
            logger.warning("Semantic memory unavailable: %s", exc)

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
    messages = build_messages(goal, session_id, rag_sources=rag_sources, memory_enabled=memory_enabled)
    tool_schemas = _tool_schemas_for(goal, web_search_enabled=web_search_enabled)

    if image_urls:
        messages[-1]["content"] = [{"type": "text", "text": goal}] + [
            {"type": "image_url", "image_url": {"url": url}} for url in image_urls
        ]

    state = ExecutionState()
    prepared_final_response = False
    while should_continue_execution(state, min(MAX_ITERATIONS, execution_plan.max_tool_rounds)):
        state.round_number += 1
        response = None
        for retry in range(MAX_RETRIES + 1):
            try:
                response = client.chat.completions.create(
                    model=VISION_MODEL if image_urls else MODEL,
                    messages=messages,
                    **({"tools": tool_schemas, "tool_choice": "auto"} if tool_schemas else {}),
                    temperature=0.4,
                )
                break
            except Exception as exc:
                logger.warning("Model request failed (retry %s/%s, tools=%s): %s", retry, MAX_RETRIES, [s.get("function", {}).get("name") for s in tool_schemas], exc)
                if retry < MAX_RETRIES:
                    time.sleep(2 ** retry)
        if response is None:
            return "❌ Model/API request failed after retries. Model/tool schema compatibility issue ho sakta hai; server logs me exact error recorded hai."

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
                messages.append({
                    "role": "system",
                    "content": recovery_instruction(name, validated),
                })

    if state.consecutive_failures >= 2:
        return "⚠️ Tool execution repeatedly failed. Maine unsafe/infinite retry se bachne ke liye execution stop kar diya."
    return f"⚠️ Max tool iterations ({MAX_ITERATIONS}) reached — task incomplete reh gaya."

def stream_agent(goal, session_id="default", image_urls=None, rag_sources=None, memory_enabled=True, web_search_enabled=True):
    """Execute tools first when required, then stream the final assistant response.

    This keeps the frontend streaming endpoint compatible with calculator/web/file/MCP
    tools instead of silently disabling tool calls.
    """
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
    messages = build_messages(goal, session_id, rag_sources=rag_sources, memory_enabled=memory_enabled)
    tool_schemas = _tool_schemas_for(goal, web_search_enabled=web_search_enabled)

    if image_urls:
        messages[-1]["content"] = [{"type": "text", "text": goal}] + [
            {"type": "image_url", "image_url": {"url": url}} for url in image_urls
        ]

    # Fast path: ordinary conversational messages do not need a non-streaming
    # tool-selection round. Stream the model response directly so simple messages
    # require one Groq request instead of two.
    if not tool_schemas:
        # Streaming connections can fail transiently (provider 5xx/429,
        # connection resets, or short-lived network errors). Retry only when
        # no tokens have reached the client yet; once partial output exists,
        # retrying would duplicate text in the conversation.
        for retry in range(MAX_RETRIES + 1):
            parts = []
            try:
                stream = client.chat.completions.create(
                    model=VISION_MODEL if image_urls else MODEL,
                    messages=messages,
                    temperature=0.4,
                    stream=True,
                )
                for chunk in stream:
                    delta = chunk.choices[0].delta.content if chunk.choices else None
                    if delta:
                        parts.append(delta)
                        yield delta
                answer = "".join(parts)
                if answer:
                    save_turn(session_id, goal, answer)
                return
            except Exception as exc:
                logger.warning(
                    "Fast streaming request failed (retry %s/%s): %s",
                    retry,
                    MAX_RETRIES,
                    exc,
                )
                status_code = getattr(exc, "status_code", None)
                non_retryable = status_code is not None and 400 <= status_code < 500 and status_code != 429
                if parts or non_retryable or retry >= MAX_RETRIES:
                    logger.exception("Fast streaming request failed permanently")
                    yield "❌ Streaming request failed."
                    return
                time.sleep(2 ** retry)

    state = ExecutionState()
    prepared_final_response = False
    while should_continue_execution(state, min(MAX_ITERATIONS, execution_plan.max_tool_rounds)):
        state.round_number += 1
        response = None
        for retry in range(MAX_RETRIES + 1):
            try:
                request_kwargs = {
                    "model": VISION_MODEL if image_urls else MODEL,
                    "messages": messages,
                    "temperature": 0.4,
                }
                if tool_schemas:
                    request_kwargs.update({"tools": tool_schemas, "tool_choice": "auto"})
                response = client.chat.completions.create(**request_kwargs)
                break
            except Exception as exc:
                logger.warning("Streaming preparation request failed (retry %s/%s, tools=%s): %s", retry, MAX_RETRIES, [s.get("function", {}).get("name") for s in tool_schemas], exc)
                if retry < MAX_RETRIES:
                    time.sleep(2 ** retry)
        if response is None:
            yield "❌ Model/API request failed after retries. Model/tool schema compatibility issue ho sakta hai; server logs me exact error recorded hai."
            return

        msg = response.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            prepared_final_response = True
            break

        for call in msg.tool_calls:
            name = call.function.name
            state.tool_calls += 1
            state.last_tool = name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
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
                messages.append({
                    "role": "system",
                    "content": recovery_instruction(name, validated),
                })

    if state.consecutive_failures >= 2:
        yield "⚠️ Tool execution repeatedly failed."
        return
    if not prepared_final_response and state.round_number >= min(MAX_ITERATIONS, execution_plan.max_tool_rounds):
        yield "⚠️ Max tool iterations reached — task incomplete reh gaya."
        return

    # The final streaming call can also fail transiently. As above, retry
    # only before any token is emitted so the client never receives duplicate
    # partial answers.
    for retry in range(MAX_RETRIES + 1):
        parts = []
        try:
            stream = client.chat.completions.create(
                model=VISION_MODEL if image_urls else MODEL,
                messages=messages,
                temperature=0.4,
                stream=True,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    parts.append(delta)
                    yield delta
            answer = "".join(parts)
            if answer:
                save_turn(session_id, goal, answer)
            return
        except Exception as exc:
            logger.warning(
                "Streaming final model request failed (retry %s/%s): %s",
                retry,
                MAX_RETRIES,
                exc,
            )
            status_code = getattr(exc, "status_code", None)
            non_retryable = status_code is not None and 400 <= status_code < 500 and status_code != 429
            if parts or non_retryable or retry >= MAX_RETRIES:
                logger.exception("Streaming final model request failed permanently")
                yield "❌ Streaming request failed."
                return
            time.sleep(2 ** retry)

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
