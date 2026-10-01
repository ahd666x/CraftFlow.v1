"""
MCP Tool Adapter — bridges MCP tool calls to CraftFlow ToolPlanner.

This is the core adapter that:
1. Receives MCP tool call requests
2. Gets the appropriate Django user
3. Uses ToolPlanner to validate (existence, permission, arguments)
4. Executes the tool via registry
5. Records audit log
6. Returns structured result preserving evidence/confidence/limitations
"""

import logging
import time
from typing import Any, Dict, Optional

from craftflow_ai.tools import get_registry
from craftflow_ai.orchestrator.planner import ToolPlanner
from craftflow_ai.audit import logger as audit_logger
from craftflow_ai.permissions.errors import (
    ToolNotFoundError,
    ToolPermissionError,
    ToolValidationError,
    ToolExecutionError,
    AIToolError,
)
from craftflow_ai.mcp.auth import get_user_for_request, MCPAuthError
from craftflow_ai.mcp.errors import MCPError, map_craftflow_error

logger = logging.getLogger('craftflow_ai.mcp.adapter')


class MCPToolAdapter:
    """Adapts MCP tool calls to CraftFlow ToolPlanner execution."""

    def __init__(self, user=None):
        self.registry = get_registry()
        self.user = user

    def set_user(self, user):
        """Set the user for subsequent tool calls."""
        self.user = user

    def call_tool(self, name: str, arguments: Dict[str, Any], mcp_session_id: str = '',
                  user_id: Optional[int] = None) -> Dict[str, Any]:
        """
        Execute a tool call through the CraftFlow security pipeline.

        This mirrors the exact flow in AIOrchestrator._execute_tool_call:
        1. ToolPlanner validates (existence, permission, arguments)
        2. If allowed, tool.run() executes
        3. Audit log recorded
        4. Result returned (already in ok/fail format)

        Args:
            name: Tool name
            arguments: Tool arguments (already validated as dict by MCP layer)
            mcp_session_id: MCP session/conversation ID for audit
            user_id: Optional user ID for multi-user mode

        Returns:
            Tool result dict with success/data/error structure
        """
        if self.user is None and user_id is None:
            raise MCPAuthError('No user context available')

        # Get user for this request (allows per-call user override)
        user = get_user_for_request(user_id) if user_id else self.user
        if user is None:
            raise MCPAuthError('Unable to resolve user')

        planner = ToolPlanner(self.registry, user)
        started = time.monotonic()

        # Step 1: Plan/validate through ToolPlanner (existence + permission + args)
        plan = planner.plan(name, arguments)

        if not plan:
            # Plan rejected - return structured error (matches CraftFlow behavior)
            duration_ms = int((time.monotonic() - started) * 1000)
            self._record_audit(name, arguments, user, mcp_session_id,
                              plan.status, plan.error_code, plan.reason, duration_ms)
            return plan.to_error()

        # Step 2: Execute the tool
        tool = plan.tool
        call_started = time.monotonic()

        try:
            result = tool.run(**plan.arguments)
            status = 'completed'
            error_code = ''
            error_message = ''
        except AIToolError as exc:
            result = {'success': False, 'error': exc.to_dict()}
            status = 'failed'
            error_code = exc.code
            error_message = exc.message
        except Exception as exc:
            logger.exception('Unexpected tool failure for %s', name)
            result = {
                'success': False,
                'error': {
                    'code': 'TOOL_EXECUTION_FAILED',
                    'message': 'Tool execution failed with unexpected error',
                    'detail': {'tool': name, 'exception': type(exc).__name__},
                }
            }
            status = 'failed'
            error_code = 'TOOL_EXECUTION_FAILED'
            error_message = type(exc).__name__

        duration_ms = int((time.monotonic() - call_started) * 1000)

        # Step 3: Record audit (matches AIOrchestrator behavior)
        self._record_audit(name, plan.arguments, user, mcp_session_id,
                          status, error_code, error_message, duration_ms,
                          result if status == 'completed' else None)

        return result

    def _record_audit(self, tool_name: str, arguments: Dict, user, session_id: str,
                      status: str, error_code: str, error_message: str,
                      duration_ms: int, result: Dict = None):
        """Record audit log entry (wrapper for existing audit.logger.record)."""
        try:
            audit_logger.record(
                tool_name=tool_name,
                arguments=arguments,
                user=user,
                session_id=session_id,
                status=status,
                error_code=error_code,
                error_message=error_message,
                duration_ms=duration_ms,
            )
            # Also record result summary if successful
            if result and status == 'completed':
                audit_logger.record_result(
                    tool_name, arguments, result, user=user,
                    session_id=session_id, duration_ms=duration_ms
                )
        except Exception:
            # Audit must never break main flow
            logger.exception('MCP audit log failed for tool=%s', tool_name)

    def list_tools(self, user=None) -> list:
        """List available tools for user."""
        target_user = user or self.user
        if target_user is None:
            target_user = get_user_for_request()
        registry = get_registry()
        return registry.available_for(target_user)


def create_adapter_for_initialize(init_params: dict):
    """
    Create adapter authenticated via MCP initialize params.

    Returns (adapter, user) tuple.
    """
    from craftflow_ai.mcp.auth import authenticate_initialize
    user = authenticate_initialize(init_params)
    return MCPToolAdapter(user), user