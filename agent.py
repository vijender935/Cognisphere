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
Dangerous/destructive local actions se bacho.

Important: Is project ka official GitHub repository hai: vijender935/Personal-AI-Assistant
(Correct spelling: Personal-AI-Assistant — NOT Assistance). GitHub tools use karte waqt hamesha yahi exact name use karo."""

def _extract_memory_candidate(text):
    lower = text.lower().strip()
    prefixes = ("remember that ", "yaad rakhna ", "yaad rakho ", "note that ", "remember: ", "yaad rakho:")
    for prefix in prefixes:
        if lower.startswith(prefix):
            return text.strip()[len(prefix):].strip()
    return None
