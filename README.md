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
- **🔒 Agent 安全层** — 执行前计划审查、高风险动作人工审批、输出脱敏、任务完成确认与哈希链审计日志
- **🔁 自我纠错** — 任务失败自动重试（瞬时故障退避重试、产出不合格回灌原因自修），目标未达成时增量重规划剩余步骤
- **🧭 多跳研究** — 检索 → 缺口分析 → 再检索，来源统一编号并要求 [n] 引用
- **🧪 代码自测** — 程序员 agent 生成的 Python 会在受限子进程中真实运行，失败则回灌 traceback 自我修复
- **🧠 跨会话记忆** — 偏好与事实自动沉淀并召回；研究结论先暂存，人工确认后才生效
- **📈 运行可观测** — 每次运行的 token 用量（按 agent 归属）、任务耗时、重试与失败率
- **🎨 明亮主题前端** — 白卡片 + slate 浅灰底 + 靛蓝强调色，代码块使用 GitHub 浅色高亮
- **🐳 Docker 部署** — 一键 `docker compose up`

## 1. 架构说明

### 1.1 总体架构

CanDoIt 采用“控制平面 + 执行平面 + 产物平面”的分层设计：

```text
┌──────────────────────────────── Frontend ────────────────────────────────┐
│ Vue 3 / Pinia                                                            │
│ Chat、PPT、图片、视频、Skill 管理、模型选择、SSE 时间线                   │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │ REST + SSE
┌───────────────────────────────▼──────────────────────────────────────────┐
│ FastAPI API Layer                                                        │
│ 请求校验、会话、模型、Skill、PPTX、研究缓存、MCP 配置、安全审查/审计      │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────────────┐
│ Agent Control Plane                                                     │
│ Supervisor → Plan JSON → 依赖补全 → Wave 调度 → Synthesizer             │
│                   asyncio.gather（同波次并行）                           │
├──────────────────────────────────────────────────────────────────────────┤
│ Safety Plane（backend/safety）                                           │
│ 计划审查 → 动作门禁/人工审批 → 输出审查与脱敏 → 目标完成确认 → 审计链     │
└───────────────┬───────────────────┬───────────────────────┬──────────────┘
                │                   │                       │
┌───────────────▼──────┐ ┌──────────▼─────────┐ ┌──────────▼─────────────┐
│ Built-in Workers     │ │ Skill Runtime      │ │ MCP Compatibility      │
│ research / analyst   │ │ Python Skill       │ │ stdio / streamable HTTP│
│ chart / image / code │ │ SKILL.md Package   │ │ schema → arguments     │
│ PPT pipeline         │ │ LLM-as-worker      │ │ → call_tool            │
└───────────────┬──────┘ └──────────┬─────────┘ └──────────┬─────────────┘
                └───────────────────┴───────────────────────┘
                                    │
┌───────────────────────────────────▼──────────────────────────────────────┐
│ Data & Artifact Plane                                                   │
│ Redis/内存缓存、ChromaDB、sessions、图片/图表/视频、HTML、PPTX           │
└──────────────────────────────────────────────────────────────────────────┘
```

核心原则：

- **显式 Agent Loop**：`backend/agents/orchestrator.py` 是唯一调度核心，不依赖 LangGraph 图拓扑决定执行顺序。
- **声明式注册**：内置 Agent 由 `agents.yaml` 描述，Skill 和 MCP Tool 最终都注册到统一的 `SkillRegistry`。
- **依赖驱动并行**：调度器根据 `depends_on` 计算可运行任务，同一 wave 用 `asyncio.gather` 并发执行。
- **共享状态归并**：普通结果采用列表追加语义，`skill_outputs` 采用按 Skill 名称合并语义，动态任务通过 `_add_tasks` 注入。
- **产物与服务解耦**：生成文件统一落到 `outputs/`，既供 `/outputs/` 静态访问，也作为 Docker 持久化卷。
- **安全层旁路可关**：`backend/safety/` 在四个检查点介入执行，`SAFETY_MODE=off` 时完全不介入也不记录。
- **双后端边界**：Web 后端使用 `backend.main:app`；飞书/Lark 使用独立的 `backend_lark.main:app`，两者不互相挂载路由。

### 1.2 一次请求的生命周期

1. API 将用户输入、会话历史和模型选择构造成统一 state（并分配 `run_id`）。
2. Supervisor 使用低温度模型输出结构化 Plan JSON：`step / agent / prompt / depends_on / reason`。
3. `validate_and_complete_plan()` 校验计划，并补齐必要的研究、PPT 等上下游任务。
4. **执行前审查**：丢弃引用未注册代理的步骤，按策略检查破坏性/凭据外传/注入意图；命中高风险时暂停等待人工审批。
5. Agent Loop 根据显式依赖和 Agent 的静态依赖计算 wave。
6. 每个任务执行前再过一道动作门禁；同一 wave 内并行执行，每个任务有超时、异常隔离和可选降级策略。
7. Worker 结果先做输出审查与凭据脱敏，再合并回共享 state；PPT planner 等 Worker 还可以动态注入后续任务。
8. **目标完成确认**：核对每个步骤产出与用户明确要求的交付物是否真实存在；未完成时按预算追加补救步骤。
9. 任务失败时先自我纠错：瞬时故障退避重试，产出不可用则把失败原因回灌给同一个 agent 重跑。
10. 所有任务结束后，Synthesizer 读取完整 state，生成面向用户的最终答案（未完成事项会被强制如实说明）。
11. 运行结束后抽取跨会话记忆（偏好/事实直接生效，结论待确认），并把本次运行的 token、耗时与失败情况写入运行记录。
10. 流式模式下，生命周期事件、审查结论和最终文本通过 SSE 持续推送到前端。

### 1.3 PPT 专用流水线

```text
research
   ↓
ppt_planner（大纲、主题、每页表达目标与视觉规格）
   ↓
ppt_slide × N（限流并行生成独立 section）
   ↓
ppt_assembler（统一主题、动画契约、导航、页码与进度条）
   ↓
单文件 HTML ── Playwright 截图 ── python-pptx ── PPTX
```

PPT HTML 遵循 16:9、单文件、无外部脚本依赖、逐层缓入、键盘/滚轮/触控/左右点击翻页等约束。PPTX 默认捕获动画完成后的最终帧；`keyframes` 模式可将动画过程采样成多张 PowerPoint 页面。

## 2. 本项目的 Prompt 与 Vibe 思路

### 2.1 Prompt 不是一句话，而是一套控制协议

项目将 Prompt 分为四层，每层只承担一种责任：

| 层级 | 责任 | 典型约束 |
|------|------|----------|
| Supervisor Prompt | 理解意图并规划任务 | 只输出 Plan JSON、声明依赖、选择 Agent |
| Worker Prompt | 完成单一专业任务 | 研究、分析、图表、代码、图片或单页 PPT |
| Skill Prompt | 注入可复用领域方法 | SKILL.md 指令、触发词、资源与输出格式 |
| Synthesizer Prompt | 汇总证据并面向用户表达 | 不泄漏内部状态、保留产物链接和事实边界 |

这种拆分避免一个“万能 Prompt”同时承担路由、执行和写作，降低指令冲突，也使每一层可以独立测试和替换。

### 2.2 推荐的 Vibe：像一个冷静的创意总监，也像一个可靠的工程经理

项目希望生成结果具有以下气质：

- **先理解交付物，再选择技术动作**：先判断用户要答案、研究报告、图表、PPT 还是可运行代码。
- **结构先于装饰**：先建立叙事和信息层级，再决定配色、动画与视觉风格。
- **证据和观点分离**：研究 Worker 提供来源，分析 Worker 提炼结构，Synthesizer 负责解释意义。
- **视觉有意图**：每张 PPT 只表达一个中心观点，每个图形都服务当前观点，不为了“炫”而增加组件。
- **对失败诚实但不中断体验**：搜索、视频、浏览器或模型调用失败时，保留错误信息并进入可解释的降级路径。
- **产物优先**：最终回复不仅描述“做了什么”，还应给出可打开的 HTML、图片、视频、PPTX 或代码文件。

### 2.3 一份理想的用户 Prompt

用户不需要了解 Agent 名称，只要描述目标、受众、材料、交付物与限制：

```text
请为产品评审会制作一份 8 页 PPT，受众是研发负责人。
主题是“多智能体客服系统的上线方案”，重点回答架构、风险、成本和两周实施计划。
使用暗色科技风，适合会议室投屏；关键数字需要注明来源。
同时输出可播放的 HTML 和 PPTX。
```

系统会将它转化为更细的内部任务，例如：

```json
{
  "plan": [
    {
      "step": 1,
      "agent": "research",
      "prompt": "收集多智能体客服架构、成本和风险资料",
      "depends_on": []
    },
    {
      "step": 2,
      "agent": "ppt_planner",
      "prompt": "规划面向研发负责人的 8 页评审叙事",
      "depends_on": [1]
    }
  ]
}
```

内部 Prompt 应尽量满足：明确角色、明确输入、明确输出 schema、列出硬性约束、说明失败时的 fallback；不要用大量形容词代替可验证标准。

## 3. AI 调用逻辑

### 3.1 模型适配与路由

- 模型通过 `ChatOpenAI` 连接任意 OpenAI-compatible `/v1` 接口。
- Provider 配置优先级为 `outputs/providers.yaml` → 根目录 `providers.yaml` → 旧版 `API.json`。
- 模型注册中心读取 Provider 的 `/models`，并按能力划分 chat、image、video。
- `router_model_id` 可与主对话模型分离：路由使用低温度和较小 token 预算，专业 Worker 使用各自参数。
- 图片和视频调用独立端点；视频 Provider 不支持时可降级为图片关键帧。

### 3.2 SSE 流式协议

`POST /api/chat` 和 `POST /api/generate-ppt` 使用 `text/event-stream`。服务端不是只流式输出最终文字，而是发送完整的 Agent 生命周期：

| Event | 含义 |
|-------|------|
| `phase` | 当前处于规划、执行或综合阶段 |
| `plan` | Supervisor 生成的任务计划（含被丢弃的未注册代理） |
| `agent_start` | 某个 Worker 开始执行，含任务 ID 和 PPT 页码等元数据 |
| `agent_done` | Worker 完成及结果摘要（`status=blocked` 表示被安全审查拦截） |
| `safety_review` | 一次执行审查结论：阶段、风险等级、决策与依据 |
| `approval_required` | 执行已暂停，等待人工审批（前端可直接批准/拒绝） |
| `approval_resolved` | 审批已决定，执行继续或终止 |
| `verification` | 目标完成确认结论：状态、得分、逐项核验与模型复核 |
| `remediation` | 针对未完成事项追加的补救轮次 |
| `budget_exceeded` | 触达步骤/波次/时长预算上限 |
| `heartbeat` | 长任务每 30 秒保活，避免代理或网关关闭连接 |
| `token` | Synthesizer 的增量文本 |
| `final` | 最终回复、HTML 产物列表、`run_id` 与本次安全摘要 |
| `done` | 本次流结束 |
| `error` | 可展示的错误事件 |

前端使用原生 `fetch` 读取 SSE 流，Pinia 分别维护 Agent 时间线和增量文本。Markdown 渲染采用短间隔 debounce，避免 token 高频到达时反复重排页面。

### 3.3 Function Calling / Tool Calling

项目采用分层工具调用模型，而不是把所有能力直接绑定到一个大模型：

1. **平台内部函数调用**

   Supervisor 输出结构化任务，Agent Loop 根据 `agent` 查找已注册 Worker，然后直接调用异步 Python 函数。搜索、图表、图片、视频、PPT 等属于平台可观测、可超时控制的服务调用。

2. **MCP Tool Calling**

   MCP Server 启动时执行 `list_tools`，工具的名称、描述和 JSON Schema 被转换为 Skill。执行时优先使用显式 `arguments`；没有参数时，模型根据 JSON Schema生成参数，再由 MCP Client 执行 `call_tool`。支持 `stdio` 和 `streamable_http`。

3. **Provider 原生 Function Calling 的定位**

   当前核心编排不依赖特定 Provider 的 `tool_calls` 字段，因此兼容只实现基础 Chat Completions 的服务。未来可以在单个 Worker 内使用原生 function calling，但不应绕过 Agent Loop 的权限、超时、状态归并和事件追踪。

4. **结构化输出与容错**

   Supervisor、PPT planner 和 MCP 参数构造都要求 JSON 输出；解析失败时进入明确的 fallback。Worker 异常被隔离为结构化错误，不会直接让整个 wave 崩溃。

### 3.4 并发、缓存与上下文控制

- 同一 wave 用 `asyncio.gather` 并发；PPT 单页生成额外使用 Semaphore，避免模型限流。
- 每个 Agent 类型可配置独立超时，外层 Agent Loop 再提供硬超时保护。
- 研究请求先查精确缓存，再做 embedding 相似度匹配，最后访问网络。
- 研究综合结果写入 ChromaDB，可跨会话召回。
- Worker 只接收当前任务和所需共享状态，避免把全部历史无差别塞入每次模型调用。
- 研究路径抓取到的外部文本会先做注入筛查并加上不可信内容围栏，再交给模型综合。
- 安全层的 LLM 计划评审与目标完成确认各受 `SAFETY_MAX_LLM_REVIEWS` 限制，避免审查本身失控。

### 3.5 Agent 安全与执行审查（backend/safety）

Agent 安全层是编排循环的旁路守卫，不改变 Wave 调度语义：

```text
计划生成
   │
   ├─[1] 计划审查 review_plan    未知代理丢弃、破坏性/凭据/注入意图、预算检查
   │                             └─ 高风险 → 人工审批（approval_required）
   ├─[2] 动作门禁 review_action  每个任务执行前逐条判定
   │                             └─ high → 暂停等审批；critical → 直接拦截
   ├─[3] 输出审查 review_output  Worker 返回后：密钥扫描、错误/空结果、外部内容注入
   │                             └─ 凭据在进入 state 前替换为占位符
   └─[4] 目标完成确认 verify      步骤产出 + 交付物关键词核验 + LLM 复核
                                 └─ 未完成 → 预算内追加补救步骤
   ↓
全程写入哈希链审计日志 outputs/audit/audit-YYYY-MM-DD.jsonl
```

**风险 → 决策映射**（由 `SAFETY_MODE` 决定是否真正执行）：

| 风险 | enforce | monitor |
|------|---------|---------|
| low | allow | allow（记录意图 allow） |
| medium | notify | allow（记录意图 notify） |
| high | review（人工审批） | allow（记录意图 review） |
| critical | block（拒绝执行） | allow（记录意图 block） |

关键实现要点：

- **代理能力分级**：内置 Worker 只调用模型与本地渲染，标记为 `executes=false`；Skill 包与 MCP 工具属于第三方扩展，标记为 `executes=true`，因此同一句话在不同代理下的判定不同。
- **注入防护**：`injection.py` 覆盖中英文指令覆盖、角色切换、系统分隔符伪造、工具调用伪造、提示词套取、数据外传、凭据读取、破坏性命令等规则；外部内容统一用 `<<<UNTRUSTED_EXTERNAL_CONTENT>>>` 围栏包裹。
- **审批失败即拒绝**：审批超时（默认 180s）按拒绝处理，`block` 决策不因任何配置而放行。
- **运行预算**：`AGENT_MAX_STEPS` / `AGENT_MAX_WAVES` / `AGENT_MAX_REPLANS` / `AGENT_MAX_WALL_SECONDS` 限制单次运行，补救轮次不会重启已耗尽的预算。
- **审计链**：每条记录包含 `prev_hash` 与 `hash`，`GET /api/safety/audit/verify` 可校验链是否被篡改；日志写入前对凭据脱敏。
- **前端可交互**：对话时间线内嵌审查结论与审批按钮，`🔒 安全` 选项卡提供总览开关、审批队列、审计检索、风险规则表和文本审查试运行。

### 3.6 安全 REST API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/safety/status` | 安全态势总览（模式、计数、预算、代理风险分布） |
| GET/PATCH | `/api/safety/settings` | 读取 / 修改运行期开关 |
| GET | `/api/safety/policy` | 代理风险分级与全部检测规则 |
| GET | `/api/safety/audit` | 审计日志检索（run_id / event / severity / decision / day） |
| GET | `/api/safety/audit/verify` | 校验审计哈希链完整性 |
| GET | `/api/safety/audit/{run_id}` | 单次运行的全部审计记录 |
| GET | `/api/safety/approvals` | 待审批与历史审批 |
| POST | `/api/safety/approvals/{id}` | 批准 / 拒绝一个被暂停的动作 |
| POST | `/api/safety/review` | 对任意文本做一次策略试审查（不执行任何操作） |

### 3.7 Agent 能力增强

除安全层外，编排内核还提供六项能力（均可在环境变量中开关）：

| 能力 | 行为 | 关键开关 |
|------|------|----------|
| 自我纠错重试 | 产出不可用时重跑任务；瞬时失败（429/5xx/超时/连接）指数退避后原样重试，语义失败（空产出/报错）把原因追加到任务提示词让 agent 自己修 | `AGENT_TASK_MAX_ATTEMPTS`、`AGENT_MAX_RETRIES` |
| 增量重规划 | 目标确认发现缺口时，让 supervisor 基于「已执行步骤 + 失败原因 + 已产出物 + 缺口」重新规划**剩余**步骤，模型不可用时回退到按缺口补齐 | `AGENT_LLM_REPLAN`、`AGENT_MAX_REPLANS` |
| 多跳研究 | 检索 → 缺口分析 → 再检索；已搜查询不重复，空轮改为换问法；来源编号并要求 `[n]` 引用 | `AGENT_RESEARCH_MAX_HOPS` |
| 代码自测闭环 | 生成的 Python 在受限子进程真实运行，失败回灌 traceback 修复；执行前先按注入规则筛查源码 | `AGENT_CODE_EXEC`、`AGENT_CODE_EXEC_ATTEMPTS` |
| 跨会话记忆 | 计划阶段按请求召回并注入提示词；运行后抽取记忆，偏好/事实自动生效，结论需人工确认 | `AGENT_MEMORY`、`AGENT_MEMORY_TOP_K` |
| 运行可观测 | 采集 token / 耗时 / 重试 / 失败，按 agent 归属，落盘 `outputs/runs/` | `AGENT_MODEL_PRICING`（成本估算，可选） |

**代码执行的隔离边界**（重要）：临时工作目录、环境变量白名单（provider 凭据不会出现在子进程里）、`python -I` 隔离模式、墙钟超时、输出上限，POSIX 上还有 CPU/内存/进程数 rlimit，并且执行前会用注入规则扫描源码。这**不是容器级隔离**；若对模型产出的代码不放心，设 `AGENT_CODE_EXEC=0` 关闭，或把后端整体跑在容器里。

**记忆的信任分级**：研究结论写错会静默污染后续所有回答，因此 `conclusion` 类记忆一律先进 `pending`，需在「🧠 记忆」选项卡确认后才参与召回；`fact` / `preference` 则自动生效。含密钥、密码等凭据的内容一律拒收。

### 3.8 运行历史与记忆 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/runs` | 运行记录列表（可按会话过滤） |
| GET | `/api/runs/stats` | 聚合统计：次数、调用数、token、平均耗时、失败与重试 |
| GET/DELETE | `/api/runs/{run_id}` | 单次运行详情 / 删除 |
| GET | `/api/runs/{run_id}/chain` | 执行链路 span 树（`?format=text` 返回可粘贴的文本树） |
| GET | `/api/runs/pricing` | 是否已配置模型价格 |
| GET | `/api/memory` | 记忆列表（按状态、类型筛选） |
| GET | `/api/memory/stats` | 记忆计数（生效 / 待确认 / 已否决） |
| POST | `/api/memory` | 手动添加记忆（人工添加直接生效） |
| POST | `/api/memory/search` | 语义召回试运行 |
| POST | `/api/memory/{id}/confirm` | 确认一条待确认结论 |
| POST | `/api/memory/{id}/reject` | 否决一条记忆 |
| DELETE | `/api/memory/{id}` | 删除一条记忆 |

### 3.10 可追踪的 skill 执行链路

审计日志记录**决策**、运行记录记录**总量**，但都回答不了「这次运行按什么顺序做了什么、时间花在哪」。执行链路补上这一层：每个环节是一个 span。

```text
✓ run:整个运行 [0.02s] request=搜索最新销量数据并画一个柱状图 tasks=3
├─ ✓ memory:召回长期记忆 [0.00s] recalled=2
├─ ✓ plan:supervisor 规划 [0.00s] steps=3
├─ ✓ review:计划审查 [0.00s] decision=allow risk=low
├─ ✓ task:research [1.9s] attempts=1
│  ├─ ✓ attempt:第 1 次尝试 [1.9s] ok=True
│  └─ ✓ review:输出审查 [0.00s] decision=allow
├─ 🔁 task:chart [0.4s] attempts=2
│  ├─ ✗ attempt:第 1 次尝试 [0.1s]  ⚠ HTTP 429 Too Many Requests
│  └─ ✓ attempt:第 2 次尝试 [0.3s] ok=True
└─ ✓ verify:目标完成确认 [0.1s] verdict=complete score=1.0
```

- 覆盖环节：run / plan / review（计划与输出）/ task / attempt（每次重试）/ verify / remediate / memory（召回与抽取）/ synth / 代码沙箱执行 / 预算
- 父子关系用 `ContextVar` 维护，**并行波次不会互相串父**（有并发隔离测试）
- 状态如实标注：被审查拒绝的运行，根 span 是 `blocked`，不会伪装成成功
- 查看：`GET /api/runs/{run_id}/chain`，或在前端「📈 运行」展开某次运行后直接查看并一键复制
- 链路随运行记录落盘（`outputs/runs/<run_id>.json` 的 `chain` 字段），有 span 上限保护

链路的价值不只是调试——它是唯一能看见「某一步被 `except` 静默吞掉」的地方：跨会话记忆召回因为引用了一个尚未赋值的变量而长期失效，单元测试与日志都没发现，第一次渲染链路就暴露了。

## 4. 部署步骤说明

### 4.1 环境要求

- Python 3.12+
- Node.js 22+
- Docker Engine + Docker Compose v2（容器部署时）
- 至少一个 OpenAI-compatible LLM Provider
- 可选：Redis、Tavily、支持图片/视频/Embedding 的 Provider

### 4.2 配置 Provider

推荐使用 YAML 配置：

```bash
# Windows PowerShell
Copy-Item providers.yaml.example providers.yaml

# Linux / macOS
cp providers.yaml.example providers.yaml
```

编辑 `providers.yaml`：

```yaml
providers:
  - name: primary-llm
    type: llm
    base_url: https://api.example.com/v1
    api_key: sk-your-key

  - name: image-provider
    type: image
    base_url: https://api.example.com/v1
    api_key: sk-your-key
```

`providers.yaml`、`API.json` 和 `mcp_servers.json` 均包含敏感信息，已加入 `.gitignore`。生产环境应使用 Secret 管理或只读挂载，不要写入镜像。

### 3.9 语义阈值是量出来的，不是猜的

研究缓存原先用 `0.85`，但这个数字没有依据（无注释、无测试、引入它的提交也没提）。用真实 provider 标定后（`python -m backend.tools.threshold_calibration`，SiliconFlow / `BAAI/bge-large-zh-v1.5`，每组 12 对）：

| 组 | min | p25 | 中位 | p75 | max |
|----|-----|-----|------|-----|-----|
| paraphrase（应命中） | 0.433 | 0.743 | 0.796 | 0.843 | 0.918 |
| same_topic（必须拒绝） | 0.574 | 0.659 | 0.770 | 0.820 | **0.904** |
| unrelated（必须拒绝） | 0.050 | 0.156 | 0.207 | 0.229 | 0.281 |

结论是**余弦相似度区分不了"同一个问题换个说法"和"同一话题另一个问题"**——最危险的负样本（「Python 读取大文件」↔「写入大文件」）得 0.904，比 75% 的真同义对还高；两组分布几乎完全重合。因此：

- **不能**靠调阈值解决。高相似度只当作"相关"的证据，不当作"同题"的证明
- 缓存改为两段式：`≥0.92` 近重复直接复用；`0.40 ~ 0.92` 先让模型判定是否为同一信息需求，**确认失败或判定不可用一律按未命中处理**（fail-closed），绝不静默返回另一个问题的答案
- 知识库检索与记忆召回只把候选**呈现**出来（人能看见、模型能忽略），误命中代价低，因此下限取在 unrelated 上界（0.281）之上：0.40 与 0.35
- 另外实测否掉了一个常见说法：给查询侧加 BGE 指令前缀**没有改善**（间隔 -0.474 vs -0.472），因此不加

标定结果会写入 `outputs/calibration/embedding-thresholds.json`；**换嵌入模型或换 provider 后应当重跑**——阈值是模型相关的属性，不是常数。

### 4.3 本地开发部署

后端：

```bash
python -m venv .venv

# Windows
.\.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate

pip install -r backend/requirements.txt
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

前端（新终端）：

```bash
cd frontend
npm install
npm run dev
```

访问地址：

- 前端：`http://localhost:3000`
- 后端：`http://localhost:8000`
- OpenAPI：`http://localhost:8000/docs`

也可以使用 `run_dev.bat`（Windows）或 `./run_dev.sh`（Linux/macOS）。

### 4.4 Docker Compose 部署

确认 `providers.yaml` 已配置后执行：

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f backend
```

Compose 会启动：

- `frontend`：Nginx 静态站点，端口 `3000`
- `backend`：FastAPI/Uvicorn，端口 `8000`
- `redis`：缓存服务，端口 `6379`

`./outputs`、Skill 包目录和 Provider 配置会挂载进容器。升级时可执行：

```bash
git pull
docker compose up --build -d
```

停止服务：

```bash
docker compose down
```

如需同时删除 Redis 命名卷，使用 `docker compose down -v`；该操作会清除缓存数据，请谨慎执行。

### 4.5 生产部署建议

- 只暴露前端入口，由 Nginx/Traefik 将 `/api/` 和 `/outputs/` 反向代理到后端。
- 为 SSE 关闭代理缓冲，并将读取超时设置为至少 10 分钟。
- 将 `outputs/` 挂载到持久化磁盘；它包含会话、PPT 运行记录和 ChromaDB。
- Redis 建议启用持久化和认证，不要直接暴露公网端口。
- Provider Key 使用环境 Secret、Docker Secret 或平台密钥管理服务。
- 图片、PPT 和视频生成耗时较长，应保留 SSE heartbeat，并根据模型延迟调整 Agent 超时环境变量。
- 部署后至少检查 `/api/models`、`/api/skills`、一次普通对话和一次 PPT 生成。

### 4.6 飞书/Lark 独立后端

飞书集成是平行运行时，不挂载到普通 Web 后端：

```bash
python -m uvicorn backend_lark.main:app --host 0.0.0.0 --port 8001 --reload

# 或
docker compose -f docker-compose.lark.yml up --build -d
```

## 项目结构

```
CanDoIt/
├── providers.yaml            # Provider 配置（推荐，不提交到 Git）
├── providers.yaml.example    # Provider 配置模板
├── API.json                  # 旧版 Provider 配置（兼容，不提交到 Git）
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
│   ├── config.py             # providers.yaml / API.json 解析
│   ├── prompts.py            # 集中式 Prompt 模板
│   ├── cache.py              # Redis 缓存（可选，回退到内存）
│   ├── embedding.py          # 嵌入向量客户端
│   ├── research_cache.py     # 研究缓存 + ChromaDB 知识库
│   ├── memory.py             # 跨会话长期记忆
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
│   │   ├── safety.py         # 安全审查 / 审计 / 审批 API
│   │   └── mcp.py            # MCP 工具配置
│   │
│   ├── safety/               # Agent 安全层
│   │   ├── reviewer.py       # SafetyReviewer — 四个检查点的门面
│   │   ├── policy.py         # 策略引擎：风险分级 → 决策
│   │   ├── injection.py      # 提示词注入 / 外部内容围栏
│   │   ├── verifier.py       # 目标完成确认 + 补救任务生成
│   │   ├── approvals.py      # 人在回路审批（超时即拒绝）
│   │   ├── audit.py          # 哈希链审计日志
│   │   ├── budget.py         # 步骤/波次/补救/时长预算
│   │   ├── redact.py         # 凭据脱敏
│   │   └── settings.py       # 运行期开关
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
│       ├── code_runner.py    # 受限执行生成的 Python
│       ├── code_exec_bootstrap.py # 子进程资源限制引导
│       └── ppt_run_store.py  # PPT 运行记录存储
│
├── backend/tests/
│   ├── test_safety.py        # 安全层测试（策略/审计/审批/完成确认/编排接线）
│   └── test_ppt_animation_contract.py
│
└── frontend/
    └── src/
        ├── App.vue           # 根组件
        ├── api/index.js      # API 层（含 SSE 流式）
        ├── stores/chat.js    # Pinia 状态管理
        └── components/
            ├── ChatPanel.vue       # 主聊天界面
            ├── RunHistory.vue      # 运行历史：token / 耗时 / 失败率
            ├── MemoryPanel.vue     # 长期记忆：待确认结论与生效记忆
            ├── ThinkingPanel.vue   # 智能体思考时间线（含审查/审批/完成确认）
            ├── SafetyPanel.vue     # 安全总览、审批队列、审计检索
            ├── ModelSelector.vue   # 模型选择器
            ├── ImageGenerator.vue  # 图像生成标签页
            ├── VideoGenerator.vue  # 视频生成标签页
            └── SkillManager.vue    # 技能管理面板
```

### 运行测试

```bash
python -m pytest backend/tests -q
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
| POST | `/api/skills/ppt-animation/export-pptx` | HTML → PPTX 导出 |
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
| `RESEARCH_CACHE_THRESHOLD` | 近重复命中阈值（高于该值直接复用缓存） | `0.92` |
| `RESEARCH_CACHE_SIMILARITY_FLOOR` | 候选下限（该值到命中阈值之间需模型确认同题） | `0.40` |
| `RESEARCH_CACHE_CONFIRM` | 是否启用同题确认（关闭后只有近重复命中） | `true` |
| `RETRIEVAL_SIMILARITY_THRESHOLD` | 知识库检索的相似度下限 | `0.40` |
| `EMBEDDING_MODEL` | 嵌入模型 ID | `BAAI/bge-large-zh-v1.5` |
| `SAFETY_MODE` | 安全审查模式：`off` / `monitor` / `enforce` | `enforce` |
| `SAFETY_AUTO_APPROVE` | 跳过人工审批直接放行（仅记录审计） | `false` |
| `SAFETY_LLM_REVIEW` | 静态扫描命中风险时调用 LLM 复核计划 | `true` |
| `SAFETY_GOAL_VERIFICATION` | 执行结束后做目标完成确认 | `true` |
| `SAFETY_AUTO_REMEDIATE` | 未完成时自动追加补救步骤 | `true` |
| `SAFETY_NONSTREAM_REVIEW` | 非流式运行遇到需审批动作时的处理：`allow` / `deny` / `review` | `allow` |
| `SAFETY_APPROVAL_TIMEOUT_SECONDS` | 人工审批超时（超时按拒绝） | `180` |
| `SAFETY_REDACT_OUTPUTS` | Worker 输出中的凭据入库前脱敏 | `true` |
| `SAFETY_MAX_LLM_REVIEWS` | 单次运行允许的审查类 LLM 调用次数上限 | `6` |
| `AGENT_MAX_STEPS` / `AGENT_MAX_WAVES` / `AGENT_MAX_REPLANS` | 单次运行的步骤/波次/补救轮次上限 | `32` / `12` / `1` |
| `AGENT_MAX_WALL_SECONDS` | 单次运行墙钟上限（秒） | `1800` |
| `AGENT_TASK_TIMEOUT_SECONDS` | 单个任务硬超时（秒，可按代理覆盖） | `600` |
| `AGENT_TASK_MAX_ATTEMPTS` | 单个任务的尝试次数（含首次） | `3` |
| `AGENT_MAX_RETRIES` | 单次运行允许的总重试次数 | `12` |
| `AGENT_RETRY_BASE_SECONDS` / `AGENT_RETRY_MAX_SECONDS` | 瞬时失败退避的基数与上限 | `2` / `20` |
| `AGENT_LLM_REPLAN` | 目标未达成时用 LLM 增量重规划（否则按缺口补齐） | `true` |
| `AGENT_RESEARCH_MAX_HOPS` | 研究最多追加几轮检索 | `2` |
| `AGENT_RESEARCH_QUERIES_PER_HOP` / `AGENT_RESEARCH_MAX_SOURCES` | 每轮查询数 / 引用来源上限 | 视 Tavily 而定 / `10` |
| `AGENT_CODE_EXEC` | 是否真实执行生成的 Python | `true` |
| `AGENT_CODE_EXEC_ATTEMPTS` / `AGENT_CODE_EXEC_TIMEOUT` | 自测修复次数 / 单次执行超时（秒） | `2` / `25` |
| `AGENT_CODE_EXEC_MEMORY_MB` / `AGENT_CODE_EXEC_CPU_SECONDS` | 子进程内存与 CPU 上限（POSIX） | `768` / `20` |
| `AGENT_MEMORY` | 跨会话长期记忆 | `true` |
| `AGENT_MEMORY_TOP_K` / `AGENT_MEMORY_MIN_SCORE` | 每次召回条数 / 相似度下限 | `5` / `0.35` |
| `AGENT_MODEL_PRICING` | 模型价格 JSON（美元/百万 token），配置后才估算成本 | 空 |

## 注意事项

- **API.json 包含 API 密钥**，已加入 `.gitignore`，请勿提交到仓库
- **模型 ID 格式**: `provider/model-name`（如 `deepseek-ai/DeepSeek-V3`）
- 搜索引擎使用 cn.bing.com（面向国内网络环境），可通过 `TAVILY_API_KEY` 切换为 Tavily
- 视频生成在不支持的提供商上会回退到图像关键帧
- PPT 动画预览页面 nginx 超时 10 分钟，SSE 有心跳保活
- Skill 包上传限制 200 MB
- **安全审查默认开启（`SAFETY_MODE=enforce`）**：只有「高风险」动作会被暂停等待审批；确认可信任的环境可用 `SAFETY_AUTO_APPROVE=true` 放行，或先切 `monitor` 观察一段时间
- **审计日志写入 `outputs/audit/`**，属于运行期数据，已加入 `.gitignore`，请按需做轮转与归档
- `outputs/safety/settings.json` 保存运行期开关覆盖值；删除该文件即恢复环境变量默认值
- **前端为明亮主题**：`frontend/tailwind.config.js` 把 `gray-*` / `surface-*` 两套 token 反转成浅色，因此大数字号代表更浅的表面、`text-gray-100` 代表最深文本；新增样式请沿用同一套 token（`bg-surface-*` / `text-gray-*` / `border-gray-*`），不要写死颜色。
