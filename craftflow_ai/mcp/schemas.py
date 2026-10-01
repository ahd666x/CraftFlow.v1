"""
MCP Tool Schemas — converts CraftFlow Tool definitions to MCP format.
"""

from typing import Any, Dict, List

from craftflow_ai.tools import get_registry


def tool_to_mcp_schema(tool) -> Dict[str, Any]:
    """
    Convert a CraftFlow Tool to MCP Tool schema.

    MCP Tool format:
    {
        "name": "tool_name",
        "description": "Human-readable description",
        "inputSchema": { ... JSON Schema ... }
    }
    """
    schema = tool.to_schema()
    input_schema = schema.get('input_schema', {
        'type': 'object',
        'properties': {},
        'additionalProperties': False
    })

    # Ensure inputSchema has required MCP fields
    if 'type' not in input_schema:
        input_schema['type'] = 'object'
    if 'properties' not in input_schema:
        input_schema['properties'] = {}
    if 'additionalProperties' not in input_schema:
        input_schema['additionalProperties'] = False

    return {
        'name': tool.name,
        'description': _build_mcp_description(tool),
        'inputSchema': input_schema,
    }


def _build_mcp_description(tool) -> str:
    """Build a detailed description for LLM consumption."""
    parts = [
        tool.description.strip(),
        '',
        f'Category: {tool.category}',
        f'Permission required: {tool.permission}',
        f'Read-only: {"Yes" if tool.read_only else "No"}',
    ]

    # Add argument details
    input_schema = tool.input_schema or {}
    properties = input_schema.get('properties', {})
    required = input_schema.get('required', [])

    if properties:
        parts.append('')
        parts.append('Arguments:')
        for prop_name, prop_spec in properties.items():
            prop_type = prop_spec.get('type', 'any')
            prop_desc = prop_spec.get('description', '')
            required_marker = ' (required)' if prop_name in required else ' (optional)'
            parts.append(f'  - {prop_name}: {prop_type}{required_marker}')
            if prop_desc:
                parts.append(f'    {prop_desc}')

    # Add limitations for analysis/simulation tools
    if tool.category in ('analysis', 'simulation'):
        parts.append('')
        parts.append('Limitations:')
        if tool.category == 'analysis':
            parts.append('  - Returns evidence-based findings with confidence levels')
            parts.append('  - Data gaps reported as "insufficient_data" or "unknown" with explicit limitations')
            parts.append('  - No fabricated numbers - all values traceable to source queries')
        elif tool.category == 'simulation':
            parts.append('  - Pure what-if simulation - no database writes')
            parts.append('  - Does not model station capacity or worker availability (not stored in CraftFlow)')
            parts.append('  - Priority changes only affect queue order, not production step timing')

    # Add output schema info
    output_schema = tool.output_schema or {}
    if output_schema:
        parts.append('')
        parts.append('Returns structured data with:')
        output_props = output_schema.get('properties', {})
        for key, spec in output_props.items():
            key_type = spec.get('type', 'object')
            parts.append(f'  - {key}: {key_type}')

    return '\n'.join(parts)


def get_all_mcp_tools(user=None) -> List[Dict[str, Any]]:
    """Get all MCP tool schemas for tools available to user."""
    registry = get_registry()
    tools = registry.available_for(user) if user else registry.list_tools()
    return [tool_to_mcp_schema(t) for t in tools]


def get_mcp_tool_by_name(name: str, user=None):
    """Get a single MCP tool schema by name."""
    registry = get_registry()
    tool = registry.get(name)

    # Check permission
    if user:
        from craftflow_ai.permissions.policy import has_permission
        if not has_permission(user, tool.permission):
            from craftflow_ai.permissions.errors import ToolPermissionError
            raise ToolPermissionError(
                f'User does not have permission: {tool.permission}',
                code='PERMISSION_DENIED',
                detail={'permission': tool.permission}
            )

    return tool_to_mcp_schema(tool)