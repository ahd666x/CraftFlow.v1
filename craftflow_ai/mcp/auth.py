"""
MCP Authentication — local auth for Hermes Agent.

For local stdio transport, authentication is based on a shared secret
configured via CRAFTFLOW_AI_MCP_SECRET environment variable.
"""

import os
import logging
from typing import Optional

from django.conf import settings
from django.contrib.auth import get_user_model

logger = logging.getLogger('craftflow_ai.mcp.auth')

User = get_user_model()

# Default service username for MCP
MCP_SERVICE_USERNAME = 'craftflow_mcp_service'


class MCPAuthError(Exception):
    """Authentication/authorization error."""
    pass


def get_mcp_secret() -> Optional[str]:
    """Get the MCP shared secret from settings."""
    return getattr(settings, 'CRAFTFLOW_AI_MCP_SECRET', None)


def validate_mcp_secret(provided_secret: str) -> bool:
    """Validate the provided secret against configured secret."""
    expected = get_mcp_secret()
    if not expected:
        logger.warning('CRAFTFLOW_AI_MCP_SECRET not configured')
        return False
    return provided_secret == expected


def get_or_create_service_user() -> User:
    """Get or create the dedicated MCP service user."""
    try:
        user = User.objects.get(username=MCP_SERVICE_USERNAME)
    except User.DoesNotExist:
        # Create service user with full permissions (manager groups)
        user = User.objects.create_user(
            username=MCP_SERVICE_USERNAME,
            password=None,  # No password - auth via secret only
            is_active=True,
            is_staff=False,
            is_superuser=False,
        )
        # Add to manager groups (1, 2, 3) for full tool access
        from django.contrib.auth.models import Group
        manager_groups = Group.objects.filter(name__in=['1', '2', '3'])
        user.groups.set(manager_groups)
        logger.info('Created MCP service user with manager permissions')
    return user


def authenticate_initialize(params: dict) -> User:
    """
    Authenticate MCP initialize request.

    Expected params:
    {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "hermes", "version": "1.0"},
        "authorization": "Bearer <secret>"  # optional, for explicit auth
    }
    """
    secret = None

    # Check for explicit authorization header in params
    auth = params.get('authorization') or params.get('Authorization')
    if auth and isinstance(auth, str):
        if auth.startswith('Bearer '):
            secret = auth[7:].strip()
        else:
            secret = auth.strip()

    # Also check in clientInfo for compatibility
    if not secret:
        client_info = params.get('clientInfo') or {}
        secret = client_info.get('mcp_secret')

    if not secret:
        # Fall back to environment variable for local development
        secret = os.environ.get('CRAFTFLOW_AI_MCP_SECRET')

    if not secret or not validate_mcp_secret(secret):
        logger.warning('MCP authentication failed: invalid or missing secret')
        raise MCPAuthError('Invalid authentication secret')

    # Return service user (for now single-user; multi-user would need user_id in params)
    return get_or_create_service_user()


def get_user_for_request(user_id: Optional[int] = None) -> User:
    """
    Get Django user for a tool call request.

    For single-service-user mode, returns the service user.
    For future multi-user mode, would look up user_id.
    """
    if user_id:
        try:
            return User.objects.get(pk=user_id, is_active=True)
        except User.DoesNotExist:
            raise MCPAuthError(f'User {user_id} not found or inactive')
    return get_or_create_service_user()