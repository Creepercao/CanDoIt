# CanDoIt — 多智能体协作平台

一个基于 **Agent Loop** 编排模式的多智能体协作 Web 应用。监督者 LLM 将用户请求拆解为分步执行计划，按波次并行分派给专业 Worker 智能体（研究员、分析师、图表师、画师、视频师、程序员、PPT 流水线），并实时流式展示智能体工作进度。

## 功能特性

- **🧠 智能计划分解** — 监督者 LLM 分析请求，生成分步执行计划（Plan Mode），按依赖关系分组为波次并行执行
- **🔍 联网搜索** — 研究智能体多轮搜索 + 网页抓取，获取实时数据；支持 Bing 和 Tavily API
- **📊 数据图表** — 分析师从文本中提取结构化数据，图表师渲染 matplotlib 可视化图表（支持中文）
- **🎨 文生图 / 🎬 文生视频** — 调用 AI 模型生成图像和视频
- **💻 代码生成** — 编程智能体生成代码
- **📽️ PPT 自动生成** — 从研究到幻灯片全自动流水线：规划大纲 → 并行生成各页 → 组装为动画 HTML 演示，支持导出 PPTX
- **🔌 Skill 插件系统** — 支持 Python 模块技能和标准 SKILL.md 包两种格式，放入 `backend/skills/` 即自动发现注册
- **🔗 MCP 兼容层** — 将外部 MCP (Model Context Protocol) 工具桥接为平台 Skill 代理
- **🧪 研究缓存与知识库** — 语义查询缓存（embedding 相似度匹配）+ ChromaDB 跨会话知识库
- **💬 会话管理** — 创建/切换/删除会话，进度持久化
- **📡 SSE 实时流式** — 前端实时展示智能体思考过程和生成结果
- **🖼️ PPTX 导出** — Playwright 渲染 HTML 幻灯片为高质量 PPTX 文件
- **🐳 Docker 部署** — 一键 `docker compose up`

## 快速开始

### 环境要求

- Python 3.12+
- Node.js 22+
- （可选）Docker & Docker Compose

### 本地开发

```bash
# 1. 配置 API
# 编辑 API.json，填入你的 LLM 提供商信息：
# {
#     name: "你的提供商",
#     base_url: "https://api.example.com/v1",
#     APIkey: "sk-xxxxxxxxxxxxxxxx"
# }

# 2. 启动后端
pip install -r backend/requirements.txt
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# 3. 启动前端（新终端）
cd frontend && npm install && npm run dev
```

或使用便捷脚本：
- **Windows**: `run_dev.bat`
- **Linux/macOS**: `./run_dev.sh`

前端 → `http://localhost:3000`
后端 → `http://localhost:8000`

### Docker 部署

```bash
docker compose up --build
```

- 前端 → `http://localhost:3000`（nginx 托管静态文件 + 反向代理 `/api/`）
- 后端 → `http://localhost:8000`
- Redis 缓存（可选）

## 项目结构

```
CanDoIt/
├── API.json                  # LLM 提供商配置（不提交到 Git）
├── mcp_servers.json          # MCP 工具服务器配置（可选）
├── docker-compose.yml        # Docker 编排
├── Dockerfile.backend
├── Dockerfile.frontend
├── outputs/                  # 生成文件：图表/图像/视频/PPT 帧/ChromaDB/会话
│   ├── chroma_db/            # ChromaDB 向量知识库
│   └── sessions/             # 会话持久化文件
│
├── backend/
│   ├── main.py               # FastAPI 入口（路由组装）
│   ├── config.py             # API.json 解析
│   ├── prompts.py            # 集中式 Prompt 模板
│   ├── cache.py              # Redis 缓存（可选，回退到内存）
│   ├── embedding.py          # 嵌入向量客户端
│   ├── research_cache.py     # 研究缓存 + ChromaDB 知识库
│   ├── mcp_adapter.py        # MCP 兼容层
│   ├── sessions.py           # 会话管理
│   │
│   ├── agents/
│   │   ├── orchestrator.py   # Agent Loop 核心编排器（分波次并行执行）
│   │   ├── builtin_workers.py# 内置 Worker 函数
│   │   ├── graph.py          # 兼容性包装器
│   │   ├── agent_loader.py   # 从 agents.yaml 加载代理
│   │   └── agents.yaml       # 声明式代理配置
│   │
│   ├── api/                  # 模块化 API 路由
│   │   ├── chat.py           # 聊天 SSE 流式
│   │   ├── models.py         # 模型列表
│   │   ├── skills.py         # 技能管理
│   │   ├── packages.py       # Skill 包安装/卸载
│   │   ├── sessions.py       # 会话 CRUD
│   │   ├── research_cache.py # 研究缓存 API
│   │   ├── pptx.py           # PPTX 导出
│   │   ├── ppt_runs.py       # PPT 运行追踪
│   │   └── mcp.py            # MCP 工具配置
│   │
│   ├── skills/               # Skill 插件目录
│   │   ├── base.py           # Skill 基类
│   │   ├── registry.py       # SkillRegistry 自动发现
│   │   ├── translator.py     # 翻译技能（Python 模块）
│   │   ├── package_installer.py # 标准 Skill 包安装器
│   │   └── packages/         # 已安装的 Skill 包
│   │       ├── ppt-animation/     # PPT 动画 HTML
│   │       ├── 学霸笔记/          # 手写笔记本风格笔记
│   │       └── ...               # 更多 skill 包
│   │
│   ├── models/
│   │   ├── registry.py       # 模型自动发现
│   │   └── provider.py       # LLM 工厂
│   │
│   └── tools/
│       ├── web_search.py     # 网页搜索（Bing + Tavily）
│       ├── data_scraper.py   # 网页抓取 + LLM 数据提取
│       ├── chart_gen.py      # matplotlib 图表渲染
│       ├── image_gen.py      # 图像生成
│       ├── video_gen.py      # 视频生成
│       ├── ppt_export.py     # HTML → PPTX 导出
│       └── ppt_run_store.py  # PPT 运行记录存储
│
└── frontend/
    └── src/
        ├── App.vue           # 根组件
        ├── api/index.js      # API 层（含 SSE 流式）
        ├── stores/chat.js    # Pinia 状态管理
        └── components/
            ├── ChatPanel.vue       # 主聊天界面
            ├── ThinkingPanel.vue   # 智能体思考时间线
            ├── ModelSelector.vue   # 模型选择器
            ├── ImageGenerator.vue  # 图像生成标签页
            ├── VideoGenerator.vue  # 视频生成标签页
            └── SkillManager.vue    # 技能管理面板
```

## 智能体工作流（Agent Loop）

```
用户请求 → 监督者 Plan Mode → 生成分步执行计划
                                  ↓
                          ┌─ 第 1 波（无依赖，并行）─────┐
                          │  🔍 研究  🎨 画师  🎬 视频  💻 代码 │
                          └──────────────────────────────┘
                                  ↓
                          ┌─ 第 2 波（第 1 波完成后）────┐
                          │  📊 分析师  🧭 PPT 规划师        │
                          └──────────────────────────────┘
                                  ↓
                          ┌─ 第 3 波 ───────────────────┐
                          │  📈 图表师  🧩 PPT 各页并行生成  │
                          └──────────────────────────────┘
                                  ↓
                          ┌─ 第 4 波 ───────────────────┐
                          │  🎞️ PPT 组装                  │
                          └──────────────────────────────┘
                                  ↓
                          📝 综合器 → 最终回复（流式输出）
```

## Skill 技能系统

Skill 是自包含的插件模块，支持两种形式：

### A. Python 模块 Skill

在 `backend/skills/` 下创建 `.py` 文件，暴露 `SKILL` 实例即可自动注册：

```python
# backend/skills/my_skill.py
from backend.skills.base import Skill

async def my_worker(state: dict) -> dict:
    tasks = [t for t in state.get("tasks", []) if t.get("agent") == "my_skill"]
    if not tasks:
        return {"skill_outputs": {}}
    # ... 处理任务 ...
    return {"skill_outputs": {"my_skill": [{"result": "done"}]}}

SKILL = Skill(
    name="my_skill",
    display_name="My Skill",
    description="我的自定义技能",
    emoji="⚡",
    worker=my_worker,
    depends_on=[],  # 空 = 并行执行；非空 = 串行
)
```

### B. 标准 Skill 包（SKILL.md 格式）

遵循 [agentskills.io](https://agentskills.io) 规范的 zip 包，含 `SKILL.md`（YAML frontmatter + Markdown 指令）。可通过前端 Skill Manager 上传安装，或直接放入 `backend/skills/packages/`。

### Skill API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/skills` | 列出所有技能 |
| GET | `/api/skills/{name}` | 查看技能详情 |
| POST | `/api/skills/{name}/toggle?enabled=true/false` | 启用/禁用技能 |
| POST | `/api/skills/packages/install` | 上传安装 Skill 包（zip） |
| DELETE | `/api/skills/packages/{name}` | 卸载 Skill 包 |

### 其他 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat` | 发送消息（SSE 流式响应） |
| GET | `/api/models` | 获取可用模型列表 |
| POST | `/api/models/refresh` | 刷新模型列表 |
| GET/POST | `/api/sessions` | 会话列表 / 创建 |
| GET/PUT/DELETE | `/api/sessions/{id}` | 会话详情 / 保存 / 删除 |
| GET/POST/DELETE | `/api/research/cache` | 研究缓存管理 |
| POST | `/api/pptx/export` | HTML → PPTX 导出 |
| GET/POST | `/api/mcp/tools` | MCP 工具管理 |

## 技术栈

| 层级 | 技术 |
|------|------|
| 智能体编排 | Agent Loop (asyncio wave 并行)，LangChain |
| 后端 | FastAPI, Pydantic |
| 前端 | Vue 3, Pinia, TailwindCSS, Vite |
| 图表 | matplotlib（思源黑体中文支持） |
| PPT 导出 | Playwright (Chromium), python-pptx |
| 网页抓取 | trafilatura, BeautifulSoup4 |
| 向量知识库 | ChromaDB |
| 嵌入向量 | Provider `/v1/embeddings` 端点（BAAI/bge-large-zh-v1.5） |
| 缓存 | Redis（可选），内存 LRU 降级 |
| 部署 | Docker, Nginx |

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `REDIS_URL` | Redis 连接地址 | `redis://redis:6379/0` |
| `TAVILY_API_KEY` | Tavily 搜索 API 密钥（可选） | 空（使用 Bing） |
| `RESEARCH_CACHE_TTL` | 研究缓存 TTL（秒） | `86400`（24h） |
| `RESEARCH_CACHE_THRESHOLD` | 语义匹配相似度阈值 | `0.85` |
| `EMBEDDING_MODEL` | 嵌入模型 ID | `BAAI/bge-large-zh-v1.5` |

## 注意事项

- **API.json 包含 API 密钥**，已加入 `.gitignore`，请勿提交到仓库
- **模型 ID 格式**: `provider/model-name`（如 `deepseek-ai/DeepSeek-V3`）
- 搜索引擎使用 cn.bing.com（面向国内网络环境），可通过 `TAVILY_API_KEY` 切换为 Tavily
- 视频生成在不支持的提供商上会回退到图像关键帧
- PPT 动画预览页面 nginx 超时 10 分钟，SSE 有心跳保活
- Skill 包上传限制 200 MB

## Feishu/Lark Dedicated Backend

The Feishu/Lark integration is a parallel backend variant instead of a route
mounted into the normal web backend.

```bash
python -m uvicorn backend_lark.main:app --host 0.0.0.0 --port 8001 --reload
```

Docker:

```bash
docker compose -f docker-compose.lark.yml up --build
```

Dedicated Feishu/Lark endpoints:

| Agent | Endpoint | Purpose |
|------|----------|---------|
| Research Agent | `POST /api/agents/research` | Research and summarize a topic or Feishu context |
| Document Agent | `POST /api/agents/document` | Summarize, rewrite, FAQ, and knowledge extraction |
| PPT Agent | `POST /api/agents/ppt` | Generate local HTML/PPT decks and return publish actions |
| Meeting Agent | `POST /api/agents/meeting` | Summarize transcripts into minutes, decisions, and action items |
| Data Report Agent | `POST /api/agents/data-report` | Analyze Sheet/Base data and produce report-style conclusions |
| Automation Agent | `POST /api/agents/automation` | Convert results into task/workflow-style action plans |

Feishu/Lark event adapter:

```http
POST /api/feishu/events
POST /api/feishu/dispatch
```

See `docs/lark-agents-service.md` for the dedicated backend contract.
