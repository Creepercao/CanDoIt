# MCP Compatibility

The platform keeps its existing Agent Loop and SkillRegistry. MCP is added as a
tool/resource compatibility layer:

```text
User -> Supervisor -> Agent Loop
  -> built-in workers
  -> local package skills
  -> MCP tools exposed as skills
```

## Enable MCP servers

Copy `mcp_servers.example.json` to `mcp_servers.json`, enable the servers you
want, and restart the backend.

You can also provide the same JSON through the `MCP_SERVERS` environment
variable. The app supports common desktop-style keys:

```json
{
  "servers": {
    "search": {
      "enabled": true,
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-brave-search"],
      "env": {
        "BRAVE_API_KEY": "..."
      },
      "tags": ["search", "web", "research"]
    }
  }
}
```

At startup, each MCP tool is registered as a Skill named:

```text
mcp__{server_name}__{tool_name}
```

Use `GET /api/mcp` to inspect configured servers and loaded tools.

## What should use MCP

Good MCP candidates:

- Web/search/browser tools: research prefers MCP tools tagged `search`, `web`,
  `research`, or `browser`, then falls back to the native search implementation.
- Filesystem/document tools: keep local file access behind explicit MCP server
  configuration instead of hard-coding it into agents.
- SaaS integrations: GitHub, Slack, Notion, databases, cloud storage, and other
  external systems fit MCP better than in-process worker code.

Keep in-process workers for capabilities tightly coupled to this app:

- PPT planning, parallel slide generation, and HTML/PPTX export.
- Chart rendering, image/video generation wrappers, and final synthesis.
- App-specific cache and knowledge-base behavior.
