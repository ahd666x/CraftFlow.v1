"""
CraftFlow AI MCP Adapter — thin transport layer for Hermes Agent.

This module exposes the existing CraftFlow AI Phase 2 tools via MCP (Model Context Protocol)
without duplicating or bypassing the existing security and orchestration architecture.

Architecture:
    Hermes Agent + LLM
        ↓
    MCP (stdio)
        ↓
    CraftFlow MCP Adapter (this module)
        ↓
    ToolPlanner / existing orchestration
        ↓
    Permission + validation + read-only gate
        ↓
    Existing Tool Registry
        ↓
    Analysis / Simulation
        ↓
    Django ORM / database
"""
from craftflow_ai.mcp.server import run_server
from craftflow_ai.mcp.adapter import MCPToolAdapter

__all__ = ['run_server', 'MCPToolAdapter']