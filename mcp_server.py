"""MCP server exposing the Personal AI Assistant tool layer.

Run locally with:
    mcp run mcp_server.py --transport streamable-http
or:
    python mcp_server.py
"""
from mcp.server import MCPServer

from memory import index_document, search_rag
from multimodal import ensure_local_file
from document_parser import extract_and_limit
from tools import calculator, read_file, web_search, write_file, run_shell

mcp = MCPServer(
    "personal-ai-assistant",
    instructions="Tools for the Personal AI Assistant: calculation, web search, sandboxed files, shell, and local RAG.",
)


@mcp.tool()
def calculate(expression: str) -> str:
    """Evaluate a safe arithmetic expression."""
    return calculator(expression)


@mcp.tool()
def search_web(query: str) -> str:
    """Search the web for current information."""
    return web_search(query)


@mcp.tool()
def read_text_file(path: str) -> str:
    """Read a UTF-8 text file inside the configured file sandbox."""
    return read_file(path)


@mcp.tool()
def write_text_file(path: str, content: str) -> str:
    """Write a UTF-8 text file inside the configured file sandbox."""
    return write_file(path, content)


@mcp.tool()
def execute_command(command: str) -> str:
    """Run an allowlisted command when shell access is explicitly enabled."""
    return run_shell(command)


@mcp.tool()
def add_knowledge(source: str, content: str) -> str:
    """Chunk and index text into the local semantic RAG knowledge base."""
    return f"Indexed {index_document(source, content)} chunks from {source}."


@mcp.tool()
def add_knowledge_file(path: str) -> str:
    """Index a text file from the configured file sandbox into the RAG knowledge base."""
    candidate = ensure_local_file(path)
    return f"Indexed {index_document(path, extract_and_limit(candidate), replace_source=True)} chunks from {path}."


@mcp.tool()
def search_knowledge(query: str, limit: int = 5) -> list[dict[str, object]]:
    """Search the local semantic RAG knowledge base."""
    return search_rag(query, limit=limit)


if __name__ == "__main__":
    mcp.run(transport="stdio")
