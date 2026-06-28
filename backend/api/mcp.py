"""MCP compatibility API."""

from fastapi import APIRouter

from backend.mcp_adapter import load_mcp_server_configs, mcp_manager

router = APIRouter()


@router.get("/mcp")
async def list_mcp_status():
    configs = load_mcp_server_configs()
    return {
        "configured_servers": [
            {
                "name": item.name,
                "transport": item.transport,
                "enabled": item.enabled,
                "description": item.description,
                "tags": item.tags,
                "has_command": bool(item.command),
                "has_url": bool(item.url),
            }
            for item in configs
        ],
        "loaded_tools": [
            {
                "skill_name": skill_name,
                "server": spec.server.name,
                "tool": spec.name,
                "display_name": spec.display_name,
                "description": spec.description,
                "tags": spec.server.tags,
            }
            for skill_name, spec in mcp_manager.tools.items()
        ],
    }
