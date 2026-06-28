"""MCP compatibility layer.

This module lets the local Agent Loop consume external MCP tools without
changing the orchestration model. Configured MCP tools are exposed as ordinary
Skill instances, so the supervisor can route work to them and the existing wave
executor can run them in parallel.
"""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from langchain_core.messages import HumanMessage

from backend.models.provider import create_chat_model
from backend.skills.base import Skill

logger = logging.getLogger("mcp")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "mcp_servers.json"


@dataclass
class MCPServerConfig:
    name: str
    transport: str = "stdio"
    enabled: bool = True
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    timeout_seconds: float = 60.0


@dataclass
class MCPToolSpec:
    server: MCPServerConfig
    name: str
    display_name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def skill_name(self) -> str:
        return f"mcp__{_slug(self.server.name)}__{_slug(self.name)}"


def _slug(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip()).strip("_").lower()
    return text or "tool"


def _load_raw_config() -> dict[str, Any]:
    raw = os.environ.get("MCP_SERVERS", "").strip()
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("Ignoring invalid MCP_SERVERS JSON: %s", exc)

    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Ignoring invalid mcp_servers.json: %s", exc)

    return {}


def load_mcp_server_configs() -> list[MCPServerConfig]:
    """Load MCP server configs from env or ``mcp_servers.json``.

    Supported shape mirrors common MCP desktop configs:

    {
      "servers": {
        "my-server": {
          "transport": "stdio",
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
          "env": {},
          "tags": ["filesystem"]
        }
      }
    }
    """
    data = _load_raw_config()
    servers = data.get("servers") or data.get("mcpServers") or {}
    if isinstance(servers, list):
        iterable = [(item.get("name", f"server_{i}"), item) for i, item in enumerate(servers, 1)]
    elif isinstance(servers, dict):
        iterable = list(servers.items())
    else:
        iterable = []

    configs: list[MCPServerConfig] = []
    for name, item in iterable:
        if not isinstance(item, dict):
            continue
        configs.append(MCPServerConfig(
            name=str(item.get("name") or name),
            transport=str(item.get("transport") or ("streamable_http" if item.get("url") else "stdio")),
            enabled=bool(item.get("enabled", True)),
            command=str(item.get("command") or ""),
            args=[str(v) for v in item.get("args", [])],
            env={str(k): str(v) for k, v in (item.get("env") or {}).items()},
            url=str(item.get("url") or ""),
            description=str(item.get("description") or ""),
            tags=[str(v).lower() for v in item.get("tags", [])],
            timeout_seconds=float(item.get("timeout_seconds") or item.get("timeout", 60)),
        ))
    return configs


class MCPClientManager:
    def __init__(self) -> None:
        self.configs: dict[str, MCPServerConfig] = {}
        self.tools: dict[str, MCPToolSpec] = {}

    async def discover(self) -> list[MCPToolSpec]:
        self.configs = {}
        self.tools = {}

        for config in load_mcp_server_configs():
            if not config.enabled:
                continue
            if config.transport == "stdio" and not config.command:
                logger.warning("Skipping MCP server '%s': missing command", config.name)
                continue
            if config.transport in {"http", "streamable_http"} and not config.url:
                logger.warning("Skipping MCP server '%s': missing url", config.name)
                continue

            self.configs[config.name] = config
            try:
                specs = await self._list_tools(config)
            except Exception as exc:
                logger.warning("MCP server '%s' discovery failed: %s", config.name, exc)
                continue
            for spec in specs:
                self.tools[spec.skill_name] = spec

        logger.info("Discovered %d MCP tool(s)", len(self.tools))
        return list(self.tools.values())

    async def _list_tools(self, config: MCPServerConfig) -> list[MCPToolSpec]:
        async with self._session(config) as session:
            result = await session.list_tools()
            specs: list[MCPToolSpec] = []
            for tool in getattr(result, "tools", []) or []:
                title = getattr(tool, "title", "") or getattr(tool, "name", "")
                schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
                specs.append(MCPToolSpec(
                    server=config,
                    name=str(getattr(tool, "name", "")),
                    display_name=str(title),
                    description=str(getattr(tool, "description", "") or ""),
                    input_schema=schema if isinstance(schema, dict) else {},
                ))
            return specs

    @asynccontextmanager
    async def _session(self, config: MCPServerConfig) -> AsyncIterator[Any]:
        try:
            from mcp import ClientSession, StdioServerParameters
        except ImportError as exc:
            raise RuntimeError("MCP Python SDK is not installed. Run pip install -r backend/requirements.txt") from exc

        if config.transport == "stdio":
            from mcp.client.stdio import stdio_client
            env = dict(os.environ)
            env.update(config.env)
            params = StdioServerParameters(command=config.command, args=config.args, env=env)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
            return

        if config.transport in {"http", "streamable_http"}:
            from mcp.client.streamable_http import streamable_http_client
            async with streamable_http_client(config.url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
            return

        raise RuntimeError(f"Unsupported MCP transport: {config.transport}")

    async def call_tool(self, skill_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = self.tools.get(skill_name)
        if not spec:
            raise KeyError(f"Unknown MCP skill: {skill_name}")

        async with self._session(spec.server) as session:
            result = await session.call_tool(spec.name, arguments=arguments or {})
            return _serialize_tool_result(result)

    def preferred_research_tools(self) -> list[MCPToolSpec]:
        preferred = []
        for spec in self.tools.values():
            haystack = " ".join([
                spec.name.lower(),
                spec.description.lower(),
                " ".join(spec.server.tags),
            ])
            if any(token in haystack for token in ("search", "web", "research", "browser")):
                preferred.append(spec)
        return preferred


mcp_manager = MCPClientManager()


def _serialize_tool_result(result: Any) -> dict[str, Any]:
    content_items = []
    for item in getattr(result, "content", []) or []:
        entry = {"type": getattr(item, "type", item.__class__.__name__)}
        for attr in ("text", "mimeType", "data"):
            if hasattr(item, attr):
                entry[attr] = getattr(item, attr)
        resource = getattr(item, "resource", None)
        if resource is not None:
            entry["resource"] = {
                "uri": str(getattr(resource, "uri", "")),
                "text": getattr(resource, "text", ""),
                "mimeType": getattr(resource, "mimeType", ""),
            }
        content_items.append(entry)

    return {
        "is_error": bool(getattr(result, "isError", False)),
        "content": content_items,
        "structured": getattr(result, "structuredContent", None),
    }


def _result_text(serialized: dict[str, Any]) -> str:
    pieces = []
    if serialized.get("structured") is not None:
        pieces.append(json.dumps(serialized["structured"], ensure_ascii=False, indent=2))
    for item in serialized.get("content", []):
        if item.get("text"):
            pieces.append(str(item["text"]))
        elif item.get("resource", {}).get("text"):
            pieces.append(str(item["resource"]["text"]))
    return "\n\n".join(pieces)


async def _build_tool_arguments(state: dict, spec: MCPToolSpec, task_prompt: str) -> dict[str, Any]:
    schema = spec.input_schema or {}
    props = schema.get("properties") if isinstance(schema, dict) else {}
    required = schema.get("required") if isinstance(schema, dict) else []

    if not props:
        return {"query": task_prompt}

    prop_names = list(props.keys())
    if len(prop_names) == 1:
        return {prop_names[0]: task_prompt}

    llm = create_chat_model(
        state.get("chat_model_id") or "deepseek-ai/DeepSeek-V3",
        temperature=0.1,
        max_tokens=1200,
    )
    prompt = f"""Generate JSON arguments for this MCP tool.
Tool: {spec.name}
Description: {spec.description}
Input JSON schema:
{json.dumps(schema, ensure_ascii=False, indent=2)}

User/task request:
{task_prompt}

Return only a JSON object. Fill required fields: {required}.
"""
    resp = await llm.ainvoke([HumanMessage(content=prompt)])
    content = resp.content if hasattr(resp, "content") else str(resp)
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    fallback: dict[str, Any] = {}
    for name in prop_names:
        fallback[name] = task_prompt if name in required or not fallback else ""
    return fallback


def build_mcp_skill(spec: MCPToolSpec) -> Skill:
    async def worker(state: dict) -> dict:
        tasks = [t for t in state.get("tasks", []) if t.get("agent") == spec.skill_name]
        results = []
        for task in tasks:
            prompt = task.get("prompt") or state.get("user_request", "")
            try:
                arguments = task.get("arguments")
                if not isinstance(arguments, dict):
                    arguments = await _build_tool_arguments(state, spec, prompt)
                raw = await mcp_manager.call_tool(spec.skill_name, arguments)
                results.append({
                    "task": prompt,
                    "server": spec.server.name,
                    "tool": spec.name,
                    "arguments": arguments,
                    "result": _result_text(raw),
                    "raw": raw,
                })
            except Exception as exc:
                logger.error("MCP tool '%s' failed: %s", spec.skill_name, exc)
                results.append({
                    "task": prompt,
                    "server": spec.server.name,
                    "tool": spec.name,
                    "error": str(exc),
                })
        return {"skill_outputs": {spec.skill_name: results}}

    trigger_hint = ", ".join(spec.server.tags[:6])
    routing = (
        f"- {spec.skill_name}: MCP tool from server '{spec.server.name}'. "
        f"{spec.description or spec.server.description}"
    )
    if trigger_hint:
        routing += f" Prefer for: {trigger_hint}."

    return Skill(
        name=spec.skill_name,
        display_name=f"MCP {spec.display_name or spec.name}",
        description=spec.description or spec.server.description or f"MCP tool {spec.name}",
        emoji="🔌",
        enabled=True,
        worker=worker,
        tools={spec.name: worker},
        depends_on=[],
        prompt_contribution=routing,
        result_key="skill_outputs",
    )


async def register_mcp_skills(skill_registry: Any) -> list[Skill]:
    specs = await mcp_manager.discover()
    skills = [build_mcp_skill(spec) for spec in specs]
    for skill in skills:
        skill_registry.register(skill)
    return skills
