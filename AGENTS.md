# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project Overview

Multi-Agent Collaboration Platform — a web app where a supervisor LLM decomposes user requests into tasks, dispatches them to specialized worker agents (research, analyst, chart, image, video, code), and synthesizes the results. The frontend visualizes agent progress in real time via SSE streaming.

## Development Commands

### Local Dev

```bash
# Backend (requires Python 3.12+)
pip install -r backend/requirements.txt
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# Frontend (requires Node 22+)
cd frontend && npm install && npm run dev
```

Or use the convenience scripts:
- **Windows**: `run_dev.bat`
- **Linux/macOS**: `./run_dev.sh`

### Docker

```bash
docker compose up --build
# Frontend → http://localhost:3000 (nginx serves static + proxies /api/ to backend)
# Backend  → http://localhost:8000
```

The Docker setup mounts `./API.json` as read-only and `./outputs/` as a shared volume for generated images/charts.

### Frontend-only

```bash
cd frontend
npm run build      # production build → dist/
npm run preview    # preview production build
```

There are no backend tests or linter configurations yet.

## Architecture

### Backend (FastAPI + LangGraph)

```
backend/
├── main.py              # FastAPI app, API routes, streaming orchestrator
├── config.py            # Loads provider configs from API.json (JS-like syntax with fallback parser)
├── cache.py             # Redis cache (optional, falls back to in-memory dict)
├── agents/
│   └── graph.py         # LangGraph multi-agent state graph + all worker node implementations
├── models/
│   ├── registry.py      # Auto-discovers available models from provider /models endpoints
│   └── provider.py      # Factory for LangChain ChatOpenAI models (uses OpenAI-compatible APIs)
└── tools/
    ├── web_search.py    # Bing (cn.bing.com) HTML scraping search
    ├── data_scraper.py  # trafilatura-based page scraper + LLM data extraction for charts
    ├── chart_gen.py     # matplotlib chart rendering (bar, line, pie, scatter, etc.) with CJK fonts
    ├── image_gen.py     # Image generation via provider's /images/generations endpoint
    └── video_gen.py     # Video generation (with image keyframe fallback when unsupported)
```

**Agent graph flow (LangGraph StateGraph):**
1. `supervisor_node` — LLM decomposes user request into tasks (JSON) or returns a direct reply
2. Graph topology enforces correct dependencies:
   - **Independent workers** (image, video, code) — dispatched in parallel via `Send`
   - **Chain** (research → analyst → chart) — sequential via conditional edges: `research_worker` completes → `route_after_research` sends to `analyst_worker` (if analyst tasks exist) → `route_after_analyst` sends to `chart_worker` (if chart tasks exist)
   - All terminal nodes converge at `synthesizer`
3. `synthesizer_node` — uses `llm.astream()` to enable token-level streaming via `astream_events`
4. The streaming path (`_stream_chat`) uses `multi_agent_graph.astream_events()` and maps LangGraph events to SSE events (`phase`, `plan`, `agent_start`, `agent_done`, `token`, `final`, `done`). Workers read tasks from `state["tasks"]` filtered by agent type; all six result state fields use `Annotated[list, operator.add]` for correct parallel accumulation.

**State shape** (`AgentState` TypedDict): `messages`, `user_request`, `tasks`, `research_results`, `analyst_results`, `chart_results`, `image_results`, `video_results`, `code_results`, `final_response`, plus model ID selections.

**Provider config**: `API.json` uses a JS-object-like format (`{name: "...", base_url: "...", APIkey: "..."}`). The config parser tries standard JSON first, then falls back to regex-based parsing. Model types are classified by keyword matching on model IDs (image: stable/diffusion/flux etc., video: cogvideo/svd/animate etc., rest: chat).

**Cache**: Redis at `REDIS_URL` env var (defaults to `redis://redis:6379/0` in Docker). Falls back to in-memory dict. Keys include supervisor routing decisions (TTL 300s) — not yet used for LLM response caching.

### Frontend (Vue 3 + Pinia + Vite + TailwindCSS)

```
frontend/src/
├── main.js              # App entry, Pinia setup
├── App.vue              # Shell: sidebar nav, top model bar, tab-based content
├── api/index.js         # Axios API layer (SSE streaming via raw fetch)
├── stores/chat.js       # Pinia store — messages, model lists, streaming state, localStorage persistence
├── utils/markdown.js    # markdown-it + highlight.js setup
└── components/
    ├── ChatPanel.vue    # Main chat UI with markdown rendering, chart/image display
    ├── ThinkingPanel.vue # Collapsible agent thinking timeline
    ├── ModelSelector.vue # Model dropdown per type (chat/image/video)
    ├── ImageGenerator.vue # Standalone image generation tab
    ├── VideoGenerator.vue # Standalone video generation tab
    ├── ImageLightbox.vue
    └── AgentStatus.vue   # Live agent status badges (legacy, not used in current SSE flow)
```

**Data flow**: `ChatPanel` → `store.sendChatMessage()` → `api.sendMessage(stream=true)` → SSE events update `store.thinkSteps` and `store.currentResponse` reactively → `ChatPanel` renders markdown + ThinkingPanel timeline.

**Model selection** persists to `localStorage` with `multiagent_` prefix.

**Vite config** proxies `/api` to `http://127.0.0.1:8000` in dev. In Docker, nginx handles this proxy.

### Docker Compose

Three services: `redis` (7-alpine), `backend` (Python with uvicorn), `frontend` (nginx serving built Vue app + reverse proxy to backend). Redis healthcheck gates backend startup. Backend mounts `API.json:ro` and `outputs/`.

## Key Dependencies

| Purpose | Package |
|---------|---------|
| Agent orchestration | `langgraph`, `langchain`, `langchain-openai` |
| Web scraping | `trafilatura`, `beautifulsoup4`, `lxml` |
| Chart rendering | `matplotlib` (with CJK font support) |
| Caching | `redis` (optional) |
| Frontend framework | Vue 3, Pinia, TailwindCSS |
| Markdown rendering | `markdown-it`, `highlight.js` |
| HTTP client | `axios` (frontend), `httpx` (backend) |

## Important Notes

- **在引入新技术或新功能时，请优先查找是否有成熟的开源库可供使用，避免重复造轮子。**
- **API.json is mounted into Docker as read-only** — it contains provider credentials. Never hardcode API keys in source files.
- **The `outputs/` directory** stores generated images, charts, and videos. It's served at `/outputs/` and must be writable.
- **Model IDs use the format `provider/model-name`** (e.g., `deepseek-ai/DeepSeek-V3`, `stabilityai/stable-diffusion-3-5-large`). The registry auto-discovers them from each provider's `/models` endpoint.
- **Chart generation requires CJK fonts** — the Dockerfile installs `fonts-wqy-zenhei` for Chinese label support in matplotlib.
- **Web search uses cn.bing.com** (not bing.com) — scoped for accessibility from within China. Sports data has dedicated site scraping (sina, qq, 163, 7m.cn, etc.).
- **Video generation** falls back to image keyframe generation when the provider doesn't support native video endpoints.
- **The streaming path** uses `multi_agent_graph.astream_events()` — the single source of truth for agent orchestration. There is no manual duplication of graph logic.
