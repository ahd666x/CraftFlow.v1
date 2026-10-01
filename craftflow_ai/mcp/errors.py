"""
MCP Error Mapping — maps CraftFlow AI errors to MCP error codes.

MCP Error Codes (JSON-RPC 2.0):
- -32700: Parse error
- -32600: Invalid Request
- -32601: Method not found
- -32602: Invalid params
- -32603: Internal error
- -32000 to -32099: Server-defined errors
"""

from craftflow_ai.permissions.errors import (
    ToolNotFoundError,
    ToolPermissionError,
    ToolValidationError,
    ToolExecutionError,
    ToolDisabledError,
    AIDisabledError,
    AIConfigurationError,
    AIToolError,
)


# MCP standard error codes
MCP_PARSE_ERROR = -32700
MCP_INVALID_REQUEST = -32600
MCP_METHOD_NOT_FOUND = -32601
MCP_INVALID_PARAMS = -32602
MCP_INTERNAL_ERROR = -32603

# CraftFlow custom error codes (server-defined range)
MCP_PERMISSION_DENIED = -32001
MCP_SERVICE_UNAVAILABLE = -32002
MCP_AI_DISABLED = -32003
MCP_CONFIGURATION_ERROR = -32004
MCP_WRITE_TOOLS_DISABLED = -32005


class MCPError(Exception):
    """MCP protocol error with code and optional data."""

    def __init__(self, code: int, message: str, data=None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)

    def to_dict(self):
        payload = {'code': self.code, 'message': self.message}
        if self.data is not None:
            payload['data'] = self.data
        return payload


def map_craftflow_error(exc: Exception) -> MCPError:
    """Map CraftFlow AI exception to MCP error."""
    if isinstance(exc, ToolNotFoundError):
        return MCPError(
            MCP_METHOD_NOT_FOUND,
            f'Tool not found: {exc.message}',
            data={'tool': exc.detail.get('tool'), 'available': exc.detail.get('available')}
        )

    if isinstance(exc, ToolPermissionError):
        return MCPError(
            MCP_PERMISSION_DENIED,
            exc.message,
            data={
                'permission': exc.detail.get('permission'),
                'required_groups': exc.detail.get('required_groups'),
                'user_groups': exc.detail.get('user_groups'),
            }
        )

    if isinstance(exc, ToolValidationError):
        return MCPError(
            MCP_INVALID_PARAMS,
            exc.message,
            data={
                'code': exc.code,
                'detail': exc.detail,
            }
        )

    if isinstance(exc, ToolExecutionError):
        return MCPError(
            MCP_INTERNAL_ERROR,
            exc.message,
            data={'tool': exc.detail.get('tool'), 'exception': exc.detail.get('exception')}
        )

    if isinstance(exc, ToolDisabledError):
        return MCPError(
            MCP_WRITE_TOOLS_DISABLED,
            exc.message,
            data={'tool': exc.detail.get('tool')}
        )

    if isinstance(exc, AIDisabledError):
        return MCPError(
            MCP_AI_DISABLED,
            exc.message,
            data={'setting': exc.detail.get('setting')}
        )

    if isinstance(exc, AIConfigurationError):
        return MCPError(
            MCP_CONFIGURATION_ERROR,
            exc.message,
            data=exc.detail
        )

    if isinstance(exc, AIToolError):
        return MCPError(
            MCP_INTERNAL_ERROR,
            exc.message,
            data={'code': exc.code, 'detail': exc.detail}
        )

    # Unknown exception - sanitize
    return MCPError(
        MCP_INTERNAL_ERROR,
        'Internal server error',
        data={'exception_type': type(exc).__name__}
    )


def validate_json_rpc_request(request: dict) -> tuple[str, dict]:
    """Validate JSON-RPC 2.0 request structure."""
    if not isinstance(request, dict):
        raise MCPError(MCP_INVALID_REQUEST, 'Request must be a JSON object')

    jsonrpc = request.get('jsonrpc')
    if jsonrpc != '2.0':
        raise MCPError(MCP_INVALID_REQUEST, 'Invalid JSON-RPC version, expected "2.0"')

    method = request.get('method')
    if not method or not isinstance(method, str):
        raise MCPError(MCP_INVALID_REQUEST, 'Missing or invalid method')

    params = request.get('params') or {}
    if not isinstance(params, dict):
        raise MCPError(MCP_INVALID_PARAMS, 'Params must be an object')

    request_id = request.get('id')
    # id can be string, number, or null (for notifications)

    return method, params, request_id