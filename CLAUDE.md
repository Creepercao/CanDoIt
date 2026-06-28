# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在此仓库中工作时提供指导。

## 项目概述

多智能体协作平台 — 一个 Web 应用，监督者 LLM 将用户请求分解为**分步执行计划**，按**波次（wave）**分派给专业化 worker 代理（research、analyst、chart、image_gen、video_gen、code、PPT 流水线、技能包代理），并综合结果。前端通过 SSE 流实时展示各代理的进度。

## 开发命令

### 本地开发

```bash
# 后端（需要 Python 3.12+）
pip install -r backend/requirements.txt
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# 前端（需要 Node 22+）
cd frontend && npm install && npm run dev
```

或使用便捷脚本：
- **Windows**: `run_dev.bat`
- **Linux/macOS**: `./run_dev.sh`

### Docker

```bash
docker compose up --build
# 前端 → http://localhost:3000（nginx 提供静态文件，并将 /api/ 代理到后端）
# 后端 → http://localhost:8000
```

Docker 将 `./API.json` 以只读方式挂载，将 `./outputs/` 作为共享卷用于存储生成的图片/图表/视频。

### 仅前端

```bash
cd frontend
npm run build      # 生产构建 → dist/
npm run preview    # 预览生产构建
```

后端暂无测试和 linter 配置。

## 架构

### 后端（FastAPI + Agent Loop）

核心编排器已经从 LangGraph StateGraph 迁移到**显式 Agent Loop** 模式 — 不再依赖 LangGraph 的图拓扑 / Send / 条件边进行调度，而是通过 `asyncio.gather` 按波次并行执行。

```
backend/
├── main.py              # FastAPI 应用入口（仅组装路由）
├── config.py            # 从 API.json 加载 provider 配置（JS 风格语法，带降级解析器）
├── prompts.py           # 集中式 prompt 模板（supervisor + 各 worker）
├── cache.py             # Redis 缓存（可选，降级为内存 dict）
├── embedding.py         # 嵌入向量客户端（复用 provider 的 /v1/embeddings 端点）
├── research_cache.py    # 研究结果语义缓存 + ChromaDB 知识库
├── mcp_adapter.py       # MCP 兼容层 — 将外部 MCP 工具注册为 Skill 代理
├── sessions.py          # 会话管理（持久化到 outputs/sessions/）
│
├── agents/              # 代理编排
│   ├── orchestrator.py  # ★ Agent Loop 核心 — 分波次执行、结果合并、流式 SSE
│   ├── builtin_workers.py# 所有内置 worker 函数（research/analyst/chart/image/video/code/PPT）
│   ├── graph.py         # 兼容性包装器（指向 orchestrator，LangGraph 图已移除）
│   ├── agent_loader.py  # 从 agents.yaml 加载内置代理 Skill 定义
│   └── agents.yaml      # 声明式代理配置（名称、依赖、worker 映射、prompt 贡献）
│
├── api/                 # 模块化 API 路由
│   ├── __init__.py      # 共享常量（OUTPUTS_DIR, SKILL_HTML_DIR）
│   ├── chat.py          # POST /api/chat（SSE 流式）— 主入口
│   ├── models.py        # GET /api/models, POST /api/models/refresh
│   ├── skills.py        # GET /api/skills, POST /api/skills/{name}/toggle
│   ├── packages.py      # POST /api/skills/packages/install, DELETE /api/skills/packages/{name}
│   ├── sessions.py      # 会话 CRUD
│   ├── research_cache.py# GET/POST/PATCH/DELETE /api/research/cache
│   ├── pptx.py          # HTML → PPTX 导出（Playwright 截图 + python-pptx）
│   ├── ppt_runs.py      # PPT 运行记录追踪
│   └── mcp.py           # MCP 工具配置 API
│
├── skills/              # 技能系统 — 可插拔的代理扩展模块
│   ├── base.py          # Skill 基类（dataclass）
│   ├── registry.py      # SkillRegistry — 自动发现、校验、查询
│   ├── translator.py    # 翻译技能（标准 Python 模块 skill）
│   ├── package_installer.py # 标准 skill 包安装器 — zip 解压、SKILL.md 解析、LLM-as-worker
│   └── packages/        # 已安装的 skill 包（SKILL.md 格式，遵循 agentskills.io 规范）
│       ├── ppt-animation/    # PPT 动画 HTML 生成
│       ├── dynamic-archify/  # 动态架构图
│       ├── flowchart/        # 流程图生成
│       ├── network-protocol-viz/ # 网络协议可视化
│       └── 学霸笔记/          # 学霸笔记（手写笔记本风格 HTML）
│
├── models/
│   ├── registry.py      # 从 provider /models 端点自动发现可用模型
│   └── provider.py      # LangChain ChatOpenAI 模型工厂（兼容 OpenAI API）
│
└── tools/
    ├── web_search.py    # Bing（cn.bing.com）HTML 抓取搜索 + Tavily API（可选）
    ├── data_scraper.py  # 基于 trafilatura 的页面抓取 + LLM 数据提取（用于图表）
    ├── chart_gen.py     # matplotlib 图表渲染（柱状图、折线图、饼图、散点图等），支持 CJK 字体
    ├── image_gen.py     # 通过 provider 的 /images/generations 端点生成图片
    ├── video_gen.py     # 视频生成（provider 不支持时降级为图片关键帧生成）
    ├── ppt_export.py    # HTML 幻灯片 → PPTX（Playwright 截图 + python-pptx）
    └── ppt_run_store.py # PPT 运行记录持久化
```

### Agent Loop 执行模型（取代 LangGraph StateGraph）

核心位于 `agents/orchestrator.py`。执行流程：

1. **Supervisor Plan Mode** — LLM 将用户请求分解为**分步执行计划**（JSON `plan` 数组，每步含 `step`、`agent`、`prompt`、`depends_on`）
2. **Wave 分组** — 根据依赖关系将步骤分组为波次：第 1 波 = 无依赖步骤（并行执行），第 2 波 = 第 1 波完成后可执行的步骤，以此类推
3. **并行执行** — 同波次内的所有 agent 通过 `asyncio.gather` 并行运行
4. **结果合并** — 每波完成后，worker 结果通过 `merge_worker_results()` 合并入状态（`*_results` 用 list-append 语义，`skill_outputs` 用 dict 合并）
5. **Synthesizer** — 所有波次完成后，综合所有结果生成最终回复（token 级别流式输出）
6. **流式路径** — `run_agent_loop_stream()` 将每一步映射为 SSE 事件（`phase`、`plan`、`agent_start`、`agent_done`、`token`、`final`、`done`）

**状态结构**：`messages`、`user_request`、`tasks`、`plan_steps`、`research_results`、`analyst_results`、`chart_results`、`image_results`、`video_results`、`code_results`、`skill_outputs`、`final_response`，以及模型 ID 选择字段。

### 内置代理（agents.yaml + agent_loader.py）

内置代理不再硬编码在图拓扑中，而是通过 `agents/agents.yaml` 声明式定义，由 `agent_loader.py` 加载为 Skill 实例并注册到 SkillRegistry。每个代理声明 `name`、`depends_on`、`worker`（映射到 `builtin_workers.py` 中的函数）、`result_key` 等字段。

**当前内置代理：**

| 代理 | 名称 | 依赖 | 说明 |
|------|------|------|------|
| 🔍 Research | `research` | 无 | 网络搜索 + 网页抓取，可复用语义缓存 |
| 📊 Analyst | `analyst` | research | 从研究文本中提取结构化数值数据 |
| 📈 Chart Maker | `chart` | analyst | 将结构化数据渲染为 matplotlib 图表 |
| 🎨 Image Generator | `image_gen` | 无 | 创意图片/插画/照片生成 |
| 🎬 Video Generator | `video_gen` | 无 | 视频/动画生成 |
| 💻 Programmer | `code` | 无 | 编程、脚本、HTML 代码 |
| 🧭 PPT Planner | `ppt_planner` | research | 根据研究结果规划幻灯片大纲 |
| 🧩 PPT Slide Builder | `ppt_slide` | ppt_planner | 并行生成各页幻灯片 HTML |
| 🎞️ PPT Assembler | `ppt_assembler` | ppt_slide | 组装幻灯片为完整的动画 HTML 演示 |

### 技能系统（Skill System）

技能系统支持两种形式的扩展：

**A. Python 模块技能**（`backend/skills/*.py`）

传统的 Python 模块，暴露出模块级 `SKILL` 实例。SkillRegistry 启动时自动扫描发现。示例：`translator.py`。

**B. 标准 Skill 包**（`backend/skills/packages/*/`）

遵循 [agentskills.io](https://agentskills.io) 规范的 `SKILL.md` 格式包。每个包是一个含 YAML frontmatter 的 Markdown 文件 + 可选脚本/资源文件。通过以下方式安装：
- 将 zip 包放入 `skills/packages/` 并重启后端
- 通过 `/api/skills/packages/install` API 上传 zip 安装
- 前端 Skill Manager 界面上传

包技能的 worker 使用通用 LLM-as-worker 模式执行（`package_installer.py`）。

**Skill 数据结构**（`base.py`）新增字段：
- `_package_meta` — 内部元数据（source、path、version），Python 模块 skill 为 None

### MCP 兼容层（mcp_adapter.py）

将外部 MCP (Model Context Protocol) 工具暴露为平台内 Skill 实例。配置从 `mcp_servers.json` 读取，支持 stdio 和 HTTP 两种传输方式。MCP 工具在启动时自动注册到 SkillRegistry，可通过 supervisor 路由调度。

### 研究缓存与知识库（research_cache.py + embedding.py）

**方案 A — 语义查询缓存**：
- 先尝试精确哈希匹配（基于 HTTP 缓存头 `ETag` / `Last-Modified`）
- 未命中时，通过 embedding 向量做语义相似度匹配（阈值 0.85）
- TTL 默认 86400 秒（24 小时），可通过 `RESEARCH_CACHE_TTL` 环境变量配置

**方案 B — ChromaDB 知识库**：
- 嵌入式向量数据库，持久化到 `outputs/chroma_db/`
- 存储研究综合结果，跨会话可检索
- 无需外部服务

**嵌入向量客户端**（`embedding.py`）复用 provider 的 `/v1/embeddings` 端点（模型：`BAAI/bge-large-zh-v1.5`），无需额外下载模型。

### Provider 配置

`API.json` 采用 JS 对象风格的格式（`{name: "...", base_url: "...", APIkey: "..."}`）。配置解析器先尝试标准 JSON 解析，再降级为正则解析。模型类型通过模型 ID 的关键词匹配分类（图片类：stable/diffusion/flux 等，视频类：cogvideo/svd/animate 等，其余为对话类）。

**缓存**：使用 `REDIS_URL` 环境变量指定的 Redis（Docker 中默认为 `redis://redis:6379/0`）。降级方案为内存 dict。

### 会话管理（sessions.py + api/sessions.py）

会话持久化到 `outputs/sessions/` 目录（JSON 文件）。支持创建、列表、获取、保存、删除。前端通过 `localStorage` 记录当前会话 ID。

### PPT 导出（tools/ppt_export.py）

HTML 幻灯片 → PPTX 的两条路径：
- **主路径**：Playwright 渲染截图 → pptx 全屏图片幻灯片（保留 CSS 背景/SVG/布局）
- **降级路径**：HTML 文本提取 → python-pptx 文本框（Playwright 不可用时）
- 支持 `final` 模式（动画结束后的静态帧）和 `keyframes` 模式（多帧截图）

### 前端（Vue 3 + Pinia + Vite + TailwindCSS）

```
frontend/src/
├── main.js              # 应用入口，Pinia 初始化
├── App.vue              # 外层框架：侧边栏导航、顶部模型栏、标签页内容
├── api/index.js         # Axios API 层 + SSE 流式（原生 fetch）
├── stores/chat.js       # Pinia store — 消息、模型、技能、会话、localStorage 持久化
├── utils/markdown.js    # markdown-it + highlight.js 配置
└── components/
    ├── ChatPanel.vue    # 主聊天界面，支持 markdown 渲染、图表/图片显示
    ├── ThinkingPanel.vue # 可折叠的代理思考时间线
    ├── ModelSelector.vue # 按类型（chat/image/video）选择模型的下拉菜单
    ├── ImageGenerator.vue # 独立图片生成标签页
    ├── VideoGenerator.vue # 独立视频生成标签页
    ├── SkillManager.vue  # 技能管理面板 — 查看/启用/禁用/安装/卸载技能
    ├── ImageLightbox.vue
    └── AgentStatus.vue   # 代理实时状态徽章（遗留组件，当前 SSE 流中未使用）
```

**数据流**：`ChatPanel` → `store.sendChatMessage()` → `api.sendMessage(stream=true)` → SSE 事件响应式更新 `store.thinkSteps` 和 `store.currentResponse` → `ChatPanel` 渲染 markdown + ThinkingPanel 时间线。

**模型选择**和**会话 ID**持久化到 `localStorage`，键名前缀为 `multiagent_`。

**Vite 配置**在开发模式下将 `/api` 代理到 `http://127.0.0.1:8000`。在 Docker 中，nginx 负责此代理。

### Docker Compose

三个服务：`redis`（7-alpine）、`backend`（Python + uvicorn）、`frontend`（nginx 提供构建后的 Vue 应用 + 反向代理到后端）。Redis 健康检查控制后端启动顺序。后端挂载 `API.json:ro` 和 `outputs/`。

## 关键依赖

| 用途 | 包名 |
|------|------|
| 代理编排 | `langgraph`、`langchain`、`langchain-openai` |
| 网页抓取 | `trafilatura`、`beautifulsoup4`、`lxml` |
| 图表渲染 | `matplotlib`（支持 CJK 字体） |
| PPT 导出 | `python-pptx`、`playwright`（Chromium） |
| 向量知识库 | `chromadb` |
| 配置解析 | `pyyaml` |
| 嵌入向量 | `httpx`（调用 provider embeddings 端点） |
| 缓存 | `redis`（可选） |
| 前端框架 | Vue 3、Pinia、TailwindCSS |
| Markdown 渲染 | `markdown-it`、`highlight.js` |
| HTTP 客户端 | `axios`（前端）、`httpx`（后端） |

## 重要注意事项

- **完成每次任务后，提交一次 Git（commit message 使用中文），保持变更粒度小而可追溯。**
- **在引入新技术或新功能时，请优先查找是否有成熟的开源库可供使用，避免重复造轮子。**
- **Push 到 GitHub 时的 Commit Message 要使用中文。**
- **API.json 在 Docker 中以只读方式挂载** — 其中包含 provider 凭证。绝对不要在源码文件中硬编码 API 密钥。
- **`outputs/` 目录**存储生成的图片、图表、视频、PPT 帧截图和 ChromaDB 数据，通过 `/outputs/` 路径提供服务，必须可写。
- **模型 ID 格式为 `provider/model-name`**（例如 `deepseek-ai/DeepSeek-V3`、`stabilityai/stable-diffusion-3-5-large`）。模型注册中心从各 provider 的 `/models` 端点自动发现可用模型。
- **图表生成需要 CJK 字体** — Dockerfile 安装了 `fonts-wqy-zenhei` 以支持 matplotlib 中的中文标签。
- **网页搜索**使用 cn.bing.com（面向中国境内访问），同时支持 Tavily API（通过 `TAVILY_API_KEY` 环境变量启用，优化搜索效率）。
- **视频生成**在 provider 不支持原生视频端点时，降级为图片关键帧生成。
- **Agent Loop 是唯一编排路径** — `agents/graph.py` 仅为兼容性包装器，实际执行完全由 `agents/orchestrator.py` 处理。新增 agent 时只需修改 `agents.yaml` + `builtin_workers.py`。
- **技能系统**是扩展机制。新增自定义代理类型时，应通过以下方式之一：
  - Python 模块 skill（`backend/skills/my_skill.py`）— 自带 worker 函数
  - 标准 Skill 包（`backend/skills/packages/my-skill/`）— SKILL.md + LLM-as-worker
- **播放 PPT 动画时，前端 nginx 超时设为 10 分钟** — PPT 生成可能耗时较长，SSE 有心跳保活。
- **Skill 包上传限制 200 MB** — 与 `starlette.formparsers.MultiPartParser.max_file_size` 一致。
