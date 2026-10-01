"""
CraftFlow AI MCP Server — stdio transport for Hermes Agent.

This server:
1. Initializes Django on startup
2. Listens on stdin for JSON-RPC 2.0 requests
3. Responds on stdout
4. Implements MCP protocol: initialize, tools/list, tools/call, shutdown
"""

import sys
import json
import logging
import traceback
from typing import Any, Dict, Optional

# Initialize Django BEFORE importing CraftFlow modules
def setup_django():
    """Initialize Django settings."""
    import os
    import django
    from django.conf import settings

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'selvi.settings')

    # Only setup if not already configured
    if not settings.configured:
        django.setup()

    # Verify database connectivity
    from django.db import connection
    connection.ensure_connection()


# Setup Django immediately on module load
setup_django()

# Now safe to import CraftFlow modules
from craftflow_ai.mcp.adapter import MCPToolAdapter, create_adapter_for_initialize
from craftflow_ai.mcp.schemas import get_all_mcp_tools
from craftflow_ai.mcp.errors import (
    MCPError, validate_json_rpc_request, map_craftflow_error,
    MCP_METHOD_NOT_FOUND, MCP_INVALID_REQUEST, MCP_INTERNAL_ERROR
)
from craftflow_ai.mcp.auth import MCPAuthError

logger = logging.getLogger('craftflow_ai.mcp.server')

# MCP Protocol version
MCP_PROTOCOL_VERSION = '2024-11-05'

# Server info
SERVER_INFO = {
    'name': 'craftflow-ai',
    'version': '2.0.0',
}


class MCPServer:
    """MCP stdio server."""

    def __init__(self):
        self.adapter: Optional[MCPToolAdapter] = None
        self.initialized = False
        self.user = None

    def handle_request(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handle a single JSON-RPC request."""
        try:
            method, params, request_id = validate_json_rpc_request(request)
        except MCPError as exc:
            return self._error_response(None, exc)

        # Route to handler
        try:
            if method == 'initialize':
                result = self._handle_initialize(params)
            elif method == 'tools/list':
                result = self._handle_tools_list(params)
            elif method == 'tools/call':
                result = self._handle_tool_call(params)
            elif method == 'shutdown':
                result = self._handle_shutdown(params)
            elif method == 'ping':
                result = {}
            else:
                raise MCPError(MCP_METHOD_NOT_FOUND, f'Unknown method: {method}')

            return self._success_response(request_id, result)

        except MCPError as exc:
            return self._error_response(request_id, exc)
        except MCPAuthError as exc:
            return self._error_response(request_id, MCPError(MCP_INTERNAL_ERROR, str(exc)))
        except Exception as exc:
            logger.exception('Unhandled error in %s', method)
            return self._error_response(request_id, MCPError(
                MCP_INTERNAL_ERROR, 'Internal server error',
                data={'exception_type': type(exc).__name__}
            ))

    def _handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle initialize request."""
        if self.initialized:
            raise MCPError(MCP_INVALID_REQUEST, 'Already initialized')

        # Authenticate and create adapter
        self.adapter, self.user = create_adapter_for_initialize(params)
        self.initialized = True

        # Return server capabilities and info
        tools = get_all_mcp_tools(self.user)
        return {
            'protocolVersion': MCP_PROTOCOL_VERSION,
            'capabilities': {
                'tools': {
                    'listChanged': False,
                },
            },
            'serverInfo': SERVER_INFO,
            'tools': tools,  # Include tools in initialize for convenience
        }

    def _handle_tools_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle tools/list request."""
        if not self.initialized:
            raise MCPError(MCP_INVALID_REQUEST, 'Not initialized')

        tools = get_all_mcp_tools(self.user)
        return {'tools': tools}

    def _handle_tool_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle tools/call request."""
        if not self.initialized:
            raise MCPError(MCP_INVALID_REQUEST, 'Not initialized')

        name = params.get('name')
        arguments = params.get('arguments', {})

        if not name or not isinstance(name, str):
            raise MCPError(MCP_INVALID_PARAMS, 'Missing or invalid tool name')

        if not isinstance(arguments, dict):
            raise MCPError(MCP_INVALID_PARAMS, 'Arguments must be an object')

        # Extract optional user_id for multi-user support
        user_id = arguments.pop('__mcp_user_id', None)

        # Extract optional session ID for audit
        mcp_session_id = params.get('sessionId', '') or arguments.pop('__mcp_session_id', '')

        result = self.adapter.call_tool(
            name=name,
            arguments=arguments,
            mcp_session_id=mcp_session_id,
            user_id=user_id,
        )

        return result

    def _handle_shutdown(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle shutdown request."""
        self.initialized = False
        self.adapter = None
        self.user = None
        return {}

    def _success_response(self, request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        """Build JSON-RPC success response."""
        response = {'jsonrpc': '2.0', 'result': result}
        if request_id is not None:
            response['id'] = request_id
        return response

    def _error_response(self, request_id: Any, error: MCPError) -> Dict[str, Any]:
        """Build JSON-RPC error response."""
        response = {'jsonrpc': '2.0', 'error': error.to_dict()}
        if request_id is not None:
            response['id'] = request_id
        return response

    def run_stdio(self):
        """Run server reading from stdin, writing to stdout."""
        logger.info('CraftFlow AI MCP server starting on stdio')

        # Read lines from stdin
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                response = self._error_response(None, MCPError(
                    -32700, 'Parse error', data={'message': str(exc)}
                ))
                self._write_response(response)
                continue

            response = self.handle_request(request)
            if response is not None:
                self._write_response(response)

            # Exit on shutdown
            if request.get('method') == 'shutdown':
                break

        logger.info('CraftFlow AI MCP server shutting down')

    def _write_response(self, response: Dict[str, Any]):
        """Write JSON-RPC response to stdout."""
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + '\n')
        sys.stdout.flush()


def run_server():
    """Entry point for running the MCP server."""
    # Configure logging to stderr (stdout is for JSON-RPC)
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
        stream=sys.stderr,
    )

    server = MCPServer()
    try:
        server.run_stdio()
    except KeyboardInterrupt:
        logger.info('Server interrupted')
    except Exception:
        logger.exception('Server fatal error')
        sys.exit(1)