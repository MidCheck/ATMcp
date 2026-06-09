"""ATMcp — Agent Teams MCP server.

A single FastAPI process that mounts a remote (streamable-HTTP) MCP server so that
LLM agents on different devices/networks/regions can form a team and share
knowledge, memory, and tasks, while a live web dashboard shows each agent's status.
"""

__version__ = "0.1.0"
