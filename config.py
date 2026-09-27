"""Centralized configuration for the Personal AI Assistant."""
from __future__ import annotations
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("AGENT_DATA_DIR", BASE_DIR / "data")).expanduser().resolve()
FILE_ROOT = Path(os.getenv("AGENT_FILE_ROOT", DATA_DIR / "files")).expanduser().resolve()

# Qwen is used as the safe default for this application because it supports
# normal function/MCP tool calling without Groq's GPT-OSS server-side code
# execution surface. GPT-OSS can still be selected explicitly with GROQ_MODEL.
MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
MAX_ITERATIONS = max(1, int(os.getenv("MAX_ITERATIONS", "8")))
MAX_RETRIES = max(0, int(os.getenv("MAX_RETRIES", "2")))
MAX_HISTORY_MESSAGES = max(1, int(os.getenv("MAX_HISTORY_MESSAGES", "30")))
MAX_FILE_CHARS = max(1000, int(os.getenv("MAX_FILE_CHARS", "10000")))
SHELL_TIMEOUT = max(1, int(os.getenv("SHELL_TIMEOUT", "30")))
ALLOW_SHELL = os.getenv("ALLOW_SHELL", "0") == "1"
ALLOWED_SHELL_COMMANDS = {
    item.strip().split()[0]
    for item in os.getenv("ALLOWED_SHELL_COMMANDS", "pwd,ls,cat,echo").split(",")
    if item.strip()
}

def ensure_directories() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILE_ROOT.mkdir(parents=True, exist_ok=True)
