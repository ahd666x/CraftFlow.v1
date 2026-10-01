"""
CLI Entrypoint for CraftFlow AI MCP Server.

Usage:
    python -m craftflow_ai.mcp
    python -m craftflow_ai.mcp.entrypoint

Environment variables:
    CRAFTFLOW_AI_MCP_SECRET - Shared secret for authentication (required)
    DJANGO_SETTINGS_MODULE - Django settings module (default: selvi.settings)
"""

import sys
import os

# Ensure Django settings before any other imports
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'selvi.settings')

def main():
    """Main entry point."""
    from craftflow_ai.mcp.server import run_server

    # Verify secret is configured
    from django.conf import settings
    if not getattr(settings, 'CRAFTFLOW_AI_MCP_SECRET', None):
        print('ERROR: CRAFTFLOW_AI_MCP_SECRET not configured in settings', file=sys.stderr)
        print('Set it in your .env file or environment', file=sys.stderr)
        sys.exit(1)

    run_server()


if __name__ == '__main__':
    main()