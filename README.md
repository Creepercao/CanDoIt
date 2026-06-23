# CanDoIt — 多智能体协作平台

一个基于 **LangGraph** 的多智能体协作 Web 应用。监督者 LLM 将用户请求拆解为任务，分派给专业 Worker 智能体（研究员、分析师、图表师、画师、视频师、程序员），并实时流式展示智能体工作进度。

## 功能特性

- **🧠 智能任务拆解** — 监督者 LLM 自动分析请求，生成任务计划
- **🔍 联网搜索** — 研究智能体多轮搜索 + 网页抓取，获取实时数据
- **📊 数据图表** — 分析师从文本中提取结构化数据，图表师渲染可视化图表（支持中文）
- **🎨 文生图 / 🎬 文生视频** — 调用 AI 模型生成图像和视频
- **💻 代码生成** — 编程智能体生成代码
- **🔌 Skill 插件系统** — 自包含的技能模块，放入 `backend/skills/` 即可自动发现并注册
- **📡 SSE 实时流式** — 前端实时展示智能体思考过程和生成结果
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
├── docker-compose.yml        # Docker 编排
├── Dockerfile.backend
├── Dockerfile.frontend
├── outputs/                  # 生成的图表/图像/视频文件
│
├── backend/
│   ├── main.py               # FastAPI 入口、API 路由、SSE 流式传输
│   ├── config.py             # API.json 解析
│   ├── cache.py              # Redis 缓存（可选，回退到内存）
│   ├── agents/
│   │   └── graph.py          # LangGraph 多智能体状态图
│   ├── models/
│   │   ├── registry.py       # 模型自动发现
│   │   └── provider.py       # LLM 工厂
│   ├── tools/
│   │   ├── web_search.py     # 网页搜索
│   │   ├── data_scraper.py   # 网页抓取 + LLM 数据提取
│   │   ├── chart_gen.py      # matplotlib 图表渲染（支持中文）
│   │   ├── image_gen.py      # 图像生成
│   │   └── video_gen.py      # 视频生成
│   └── skills/               # Skill 插件目录
│       ├── base.py           # Skill 基类
│       ├── registry.py       # SkillRegistry 自动发现
│       └── translator.py     # 示例：翻译技能
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
            └── VideoGenerator.vue  # 视频生成标签页
```

## 智能体工作流

```
用户请求 → 监督者拆解 → 任务分派
                          ├─ 🔍 研究员 → 🔢 分析师 → 📊 图表师  （串行链）
                          ├─ 🎨 画师                         （并行）
                          ├─ 🎬 视频师                        （并行）
                          ├─ 💻 程序员                        （并行）
                          └─ [技能智能体...]                   （动态注册）
                          ↓
                        📝 整合 → 最终回复
```

## Skill 技能系统

Skill 是自包含的插件模块，放入 `backend/skills/` 目录即可自动发现。每个 Skill 可以：

- 注册新的 Worker 节点到 LangGraph
- 提供工具函数
- 贡献监督者 Prompt 描述

### 自定义 Skill 示例

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
    depends_on=[],  # 空 = 并行执行；非空 = 串行链
)
```

### Skill API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/skills` | 列出所有技能 |
| GET | `/api/skills/{name}` | 查看技能详情 |
| POST | `/api/skills/{name}/toggle?enabled=true/false` | 启用/禁用技能 |

## 技术栈

| 层级 | 技术 |
|------|------|
| 智能体编排 | LangGraph, LangChain |
| 后端 | FastAPI, Pydantic |
| 前端 | Vue 3, Pinia, TailwindCSS, Vite |
| 图表 | matplotlib（思源黑体中文支持） |
| 抓取 | trafilatura, BeautifulSoup4 |
| 缓存 | Redis（可选） |
| 部署 | Docker, Nginx |

## 注意事项

- **API.json 包含 API 密钥**，已加入 `.gitignore`，请勿提交到仓库
- **模型 ID 格式**: `provider/model-name`（如 `deepseek-ai/DeepSeek-V3`）
- 搜索引擎使用 cn.bing.com（面向国内网络环境）
- 视频生成在不支持的提供商上会回退到图像关键帧
