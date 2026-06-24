# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在此仓库中工作时提供指导。

## 项目概述

多智能体协作平台 — 一个 Web 应用，由监督者 LLM 将用户请求分解为任务，分派给专业化的 worker 代理（research、analyst、chart、image、video、code），并综合结果。前端通过 SSE 流实时展示各代理的进度。

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

### 后端（FastAPI + LangGraph）

```
backend/
├── main.py              # FastAPI 应用、API 路由、流式编排器
├── config.py            # 从 API.json 加载 provider 配置（JS 风格语法，带降级解析器）
├── cache.py             # Redis 缓存（可选，降级为内存 dict）
├── agents/
│   └── graph.py         # LangGraph 多智能体状态图 + 所有 worker 节点实现
├── skills/              # 🆕 技能系统 — 可插拔的代理扩展模块
│   ├── __init__.py      # 技能包入口
│   ├── base.py          # Skill 基类（dataclass），定义技能接口规范
│   ├── registry.py      # SkillRegistry — 自动发现、校验、查询技能模块
│   └── translator.py    # 翻译技能 — 将最终输出翻译为目标语言
├── models/
│   ├── registry.py      # 从 provider /models 端点自动发现可用模型
│   └── provider.py      # LangChain ChatOpenAI 模型工厂（兼容 OpenAI API）
└── tools/
    ├── web_search.py    # Bing（cn.bing.com）HTML 抓取搜索
    ├── data_scraper.py  # 基于 trafilatura 的页面抓取 + LLM 数据提取（用于图表）
    ├── chart_gen.py     # matplotlib 图表渲染（柱状图、折线图、饼图、散点图等），支持 CJK 字体
    ├── image_gen.py     # 通过 provider 的 /images/generations 端点生成图片
    └── video_gen.py     # 视频生成（provider 不支持时降级为图片关键帧生成）
```

### 🆕 技能系统（Skill System）

技能系统是平台的可插拔扩展机制。每个技能模块位于 `backend/skills/` 下，暴露出一个模块级的 `SKILL` 实例。`SkillRegistry` 在启动时自动发现这些实例，并集成到 LangGraph 多智能体图中。

**Skill 数据结构**（`base.py`）：

| 字段 | 说明 |
|------|------|
| `name` | 唯一标识符，如 `"translator"`，用作任务路由中的代理类型 |
| `display_name` | 人类可读标签，如 `"Translator"` |
| `description` | 单行描述，展示在 supervisor prompt 中 |
| `emoji` | 前端和 SSE 事件中显示的图标 |
| `enabled` | 是否启用（`False` 则不会加载到图中） |
| `worker` | 异步函数 `(state: dict) -> dict`，注册为 LangGraph 节点 |
| `tools` | 该技能暴露的可调用工具集 |
| `depends_on` | 上游依赖的代理类型列表。空列表 = 独立并行执行；非空 = 插入到顺序链条中 |
| `prompt_contribution` | 追加到 supervisor prompt 的文本，让 LLM 知道该代理的存在 |
| `result_key` | worker 结果写入的状态键名，默认为 `{name}_results`，新技能应使用 `skill_outputs` |

**SkillRegistry**（`registry.py`）：

- 通过 `importlib` + `pkgutil` 自动扫描 `backend.skills` 包下所有非下划线开头的模块
- 提供 `get_enabled()`、`get_node_funcs()`、`get_agent_labels()` 等图构建辅助方法
- `get_independent_agents()` — 返回可并行调度的代理集合
- `get_chain_agents()` — 返回合并后的顺序链条（基础链 `research → analyst → chart`，有依赖的技能插入对应位置）
- `build_skills_section()` — 生成 supervisor prompt 的技能描述段
- `to_api_list()` — 序列化技能元数据供 `/api/skills` 端点使用
- `skill_outputs` 使用自定义 reducer `_merge_skill_outputs` 在并行分支中安全累积

**当前已注册的技能：**

| 技能 | 名称 | 类型 | 说明 |
|------|------|------|------|
| 🌐 Translator | `translator` | 独立并行 | 将最终输出翻译为指定语言（如日语、法语、英文） |

**添加新技能的步骤：**

1. 在 `backend/skills/` 下创建新模块（如 `my_skill.py`）
2. 定义一个 `async worker(state: dict) -> dict` 函数
3. 创建模块级 `SKILL = Skill(...)` 实例，填写完整元数据
4. 重启后端即可自动发现和注册

### 代理图流程（LangGraph StateGraph）

1. `supervisor_node` — LLM 将用户请求分解为任务（JSON）或返回直接回复
2. 图拓扑结构强制正确的依赖关系：
   - **并行 workers**（image、video、code + 独立的技能代理）— 通过 `Send` 并行调度
   - **顺序链条**（research → analyst → chart + 有依赖的技能代理）— 通过条件边串联：`research_worker` 完成 → `route_after_research` 发送到 `analyst_worker`（如存在 analyst 任务）→ `route_after_analyst` 发送到 `chart_worker`（如存在 chart 任务）
   - 所有终端节点汇聚于 `synthesizer`
3. `synthesizer_node` — 使用 `llm.astream()` 实现 token 级别的流式输出（通过 `astream_events`）
4. 流式路径（`_stream_chat`）使用 `multi_agent_graph.astream_events()` 并将 LangGraph 事件映射为 SSE 事件（`phase`、`plan`、`agent_start`、`agent_done`、`token`、`final`、`done`）。Workers 从 `state["tasks"]` 中按代理类型筛选任务；所有结果状态字段使用 `Annotated[list, operator.add]` 以正确进行并行累积。技能结果写入 `state["skill_outputs"]`（dict 类型，同样使用自定义 reducer 并行累积）。

**状态结构**（`AgentState` TypedDict）：`messages`、`user_request`、`tasks`、`research_results`、`analyst_results`、`chart_results`、`image_results`、`video_results`、`code_results`、`skill_outputs`、`final_response`，以及模型 ID 选择字段。

**Provider 配置**：`API.json` 采用 JS 对象风格的格式（`{name: "...", base_url: "...", APIkey: "..."}`）。配置解析器先尝试标准 JSON 解析，再降级为正则解析。模型类型通过模型 ID 的关键词匹配分类（图片类：stable/diffusion/flux 等，视频类：cogvideo/svd/animate 等，其余为对话类）。

**缓存**：使用 `REDIS_URL` 环境变量指定的 Redis（Docker 中默认为 `redis://redis:6379/0`）。降级方案为内存 dict。缓存键包括 supervisor 路由决策（TTL 300s）—— 尚未用于 LLM 响应缓存。

### 前端（Vue 3 + Pinia + Vite + TailwindCSS）

```
frontend/src/
├── main.js              # 应用入口，Pinia 初始化
├── App.vue              # 外层框架：侧边栏导航、顶部模型栏、标签页内容
├── api/index.js         # Axios API 层（SSE 流式通过原生 fetch 实现）
├── stores/chat.js       # Pinia store — 消息、模型列表、流式状态、localStorage 持久化
├── utils/markdown.js    # markdown-it + highlight.js 配置
└── components/
    ├── ChatPanel.vue    # 主聊天界面，支持 markdown 渲染、图表/图片显示
    ├── ThinkingPanel.vue # 可折叠的代理思考时间线
    ├── ModelSelector.vue # 按类型（chat/image/video）选择模型的下拉菜单
    ├── ImageGenerator.vue # 独立图片生成标签页
    ├── VideoGenerator.vue # 独立视频生成标签页
    ├── ImageLightbox.vue
    └── AgentStatus.vue   # 代理实时状态徽章（遗留组件，当前 SSE 流中未使用）
```

**数据流**：`ChatPanel` → `store.sendChatMessage()` → `api.sendMessage(stream=true)` → SSE 事件响应式更新 `store.thinkSteps` 和 `store.currentResponse` → `ChatPanel` 渲染 markdown + ThinkingPanel 时间线。

**模型选择**持久化到 `localStorage`，键名前缀为 `multiagent_`。

**Vite 配置**在开发模式下将 `/api` 代理到 `http://127.0.0.1:8000`。在 Docker 中，nginx 负责此代理。

### Docker Compose

三个服务：`redis`（7-alpine）、`backend`（Python + uvicorn）、`frontend`（nginx 提供构建后的 Vue 应用 + 反向代理到后端）。Redis 健康检查控制后端启动顺序。后端挂载 `API.json:ro` 和 `outputs/`。

## 关键依赖

| 用途 | 包名 |
|------|------|
| 代理编排 | `langgraph`、`langchain`、`langchain-openai` |
| 网页抓取 | `trafilatura`、`beautifulsoup4`、`lxml` |
| 图表渲染 | `matplotlib`（支持 CJK 字体） |
| 缓存 | `redis`（可选） |
| 前端框架 | Vue 3、Pinia、TailwindCSS |
| Markdown 渲染 | `markdown-it`、`highlight.js` |
| HTTP 客户端 | `axios`（前端）、`httpx`（后端） |

## 重要注意事项

- **完成每次任务后，提交一次 Git（commit message 使用中文），保持变更粒度小而可追溯。**
- **在引入新技术或新功能时，请优先查找是否有成熟的开源库可供使用，避免重复造轮子。**
- **Push 到 GitHub 时的 Commit Message 要使用中文。**
- **API.json 在 Docker 中以只读方式挂载** — 其中包含 provider 凭证。绝对不要在源码文件中硬编码 API 密钥。
- **`outputs/` 目录**存储生成的图片、图表和视频，通过 `/outputs/` 路径提供服务，必须可写。
- **模型 ID 格式为 `provider/model-name`**（例如 `deepseek-ai/DeepSeek-V3`、`stabilityai/stable-diffusion-3-5-large`）。模型注册中心从各 provider 的 `/models` 端点自动发现可用模型。
- **图表生成需要 CJK 字体** — Dockerfile 安装了 `fonts-wqy-zenhei` 以支持 matplotlib 中的中文标签。
- **网页搜索使用 cn.bing.com**（而非 bing.com）— 面向中国境内访问。体育数据有专门的站点抓取（新浪、腾讯、网易、7m.cn 等）。
- **视频生成**在 provider 不支持原生视频端点时，降级为图片关键帧生成。
- **流式路径**使用 `multi_agent_graph.astream_events()` — 这是代理编排的唯一数据源，不存在手动复制图逻辑的情况。
- **技能系统**是新的主要扩展机制。新增自定义代理类型时，应通过技能模块而非直接修改 `graph.py`。技能由 `SkillRegistry` 自动发现，支持独立并行执行和顺序依赖两种编排模式。
