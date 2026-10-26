# 路线图与想法清单（持久化）

> 目的：把讨论过的想法固化下来，防止上下文压缩后丢失。
> 每条都写清「为什么做 / 做到什么程度 / 动哪里」，让新会话能直接接手。
> 最后更新：见 git log。

---

## 当前基线（接手前先确认）

- 编排：LangGraph 主干图，`backend/agents/graph_flow.py`，驱动在 `backend/agents/runner.py`
- 队列：`backend/mq/`（bus / decisions / dispatch / celery_app / worker），`MQ_BACKEND=inline|celery`
- 测试：**271 项全绿**
- 端到端：已在真实 RabbitMQ + Celery 上验证直答链路（`phase → phase → final → done`）
- RAG 相关三个组件：
  - `backend/research_cache.py` — 语义查询缓存（两段式判定，已标定）+ KnowledgeBase（ChromaDB）
  - `backend/memory.py` — 跨会话记忆（JSON + 余弦，无索引）
  - `backend/embedding.py` — 复用 provider 的 embeddings 端点

---

## A. 多智能体策略学习（明确不是完整 MARL）

### 定位与用词

**术语必须准确**：这是**多智能体策略学习（contextual bandit）**，不是 MARL。

完整的 MARL（联合策略、central critic、与可交互环境训练百万步）在本项目**不成立**：
没有可交互环境、LLM 是黑盒、单次运行成本是秒到分钟级，样本量撑不起来。

**简历/面试表述**：写「多智能体策略学习（上下文赌博机）」，不要写 MARL。
否则会被追问"训练循环怎么写的""收敛曲线呢"，答不上来。

### 范围（用户明确要求：不需要太完善，MVP 即可）

只做**离线打分器 + 决策建议**，不接管执行：

1. 一个 LinUCB / Thompson Sampling 打分器
2. 从 `outputs/runs/` 历史记录构造样本
3. 离线评估脚本：对比「LLM 规划」与「策略建议」的预期奖励
4. 接线分两步：先只记录对比，跑一段时间再考虑开关

### 决策点（可学习化的四件事）

| 决策点 | 现状 | 策略化后 |
|---|---|---|
| 任务派给哪个 agent | LLM 规划 | 候选集打分选最高 |
| 同一波并发几只 | 固定 | 按历史成功率调宽度 |
| 是否提前终止 | 预算耗尽才停 | 价值估计提前止损 |
| 是否触发补救 | 规则判定 | 按 ROI 估计 |

**MVP 只做第一项**（agent 选择），其余留作后续。

### 奖励信号（现成可用，无需新埋点）

来自 `outputs/runs/` 的 trace：

- `verification.score`（目标完成度）
- `attempts`（重试次数，越少越好）
- `duration`（耗时）
- `budget` 消耗

建议形如：`reward = score - α·attempts - β·normalized_duration`

### 上下文特征（MVP 版）

- agent 的历史成功率、平均 attempts、平均耗时
- 任务的依赖深度、上游任务数
- 预算余量
- （可选）prompt 的字符 n-gram hash 作为粗糙语义特征

**降级方案**：不用 embedding 做特征，避免依赖外部 API，保证离线可跑。

### 动哪些文件

- 新增 `backend/agents/policy.py` — 打分器（纯标准库，几十行）
- 新增 `backend/tools/policy_train.py` — 样本构建 + 离线训练 + 评估
- 在 `graph_flow.py` 的 `plan_node` 后加**只记录**的对比钩子

### 为什么这是最快的

- **不碰现有执行链路**，271 项测试不受影响
- 不依赖新的存储结构，不需要迁移数据
- 纯离线可验证，出数字快（估 2-3 小时）
- 对比层级检索：后者要改 ChromaDB 存储结构 + 重新灌数据

---

## B. 检索层级化

### 目标结构

```
文档层（原文 + 摘要）
  └─ 章节层（section summary）
       └─ 段落层（chunk，带 parent_id）
```

### 检索流程

query → 先检章节摘要（粗，定位候选文档）→ 再检段落 chunk（细）→ 按 parent 拼回上下文喂模型。

### 要解决的问题

现状 `research_cache.py` 的 `KnowledgeBase.add()`：
- 嵌入只用 `synthesis[:2000]`
- 存储用 `synthesis[:4000]`
- **嵌入和存储看的不是同一段文本，且都硬截断**

长文档的尾部内容既没被索引也没被保存，层级化真能解决这个问题。

### 动哪些文件

- 新增 chunker（按标题/段落切分）
- `research_cache.py` 的 `KnowledgeBase` 改三层集合
- 新增两级检索入口
- 需要考虑既有 `outputs/chroma_db/` 数据迁移（可先并存，新的写三层）

### 成本

半天以上。慢的原因不是算法，是**要改存储结构 + 线上检索要跟着改**。

---

## C. RAG 缺陷级问题（建议优先修，成本低）

> **状态：C1 与 C2 已完成并提交。** 见 `backend/tests/test_knowledge_base.py`。

### C1. top_k 截断发生在阈值过滤之前 ★ ✅ 已修

`backend/research_cache.py:438`：`n_results = min(top_k, 20)`
过滤在 `:464`：`if similarity < threshold: continue`

**先取 5 条候选再按阈值筛，筛完可能一条不剩**——而库里其实有匹配项，只是排在第 6 位。

修法：先多召回（如 50 条）→ 过滤 → 再截断。一行改动，纯收益。

**已实现**：`n_results = min(max(top_k * 10, 50), 200)`，过滤后 `return entries[:top_k]`。

### C2. 知识库没有去重 ✅ 已修

`:388` `entry_id = cache.hash_key(f"{query}:{_now_iso()}")` — id 带时间戳，同一问题跑两次就是两条独立记录。

修法：用 query 的 hash 做 id，或写入前先查相似度。

**已实现**：`entry_id = cache.hash_key(query)`（去掉时间戳），写入改用 `collection.upsert()`，同一问题重跑会覆盖而不是新增。

### C3. 嵌入与存储文本不一致

见 B 节。属于层级化要一起解决的根因。

---

## D. 检索质量优化（按收益排序）

| 优化 | 收益 | 成本 |
|---|---|---|
| 混合检索（向量 + BM25，RRF 融合） | 专有名词/代码标识符/精确数字提升明显，纯向量在这些上很弱 | 中（项目里已有 `token_overlap` 可作雏形） |
| Chunking + parent-child | 长文档精度提升最大 | 中（= B 节） |
| Rerank | top-3 准确率通常 +10~20 点 | 先可用 LLM 打分验证，再决定是否上 bge-reranker |
| 多查询改写 | 口语化提问召回率提升 | 低（一次 LLM 调用） |
| 时间衰减 / 元数据过滤 | `stored_at`、`source_count` 已存但检索完全没用 | 低 |

---

## E. 评估体系（前置条件）

### 现状

为**缓存阈值**做过标定（`backend/tools/threshold_calibration.py`，12 对样本），说明"阈值要量出来"这个意识已有。

但**检索质量本身没有任何离线评估**。没有评估集，上面所有优化都是拍脑袋。

### 要做什么

标注 30–50 条「query → 应命中的条目」，算 **recall@5 / MRR**。

有基线后，才知道 rerank 值不值得加、层级化有没有真提升。

**投入约一天，但它决定后面每一项优化是真提升还是自我感觉良好。**

---

## F. 架构风险（引入 MQ 后新增）

### ChromaDB 嵌入式 vs 多 worker 冲突

`PersistentClient` 是单进程写模型。

- **现状安全**：docker-compose 里 worker 用 `--pool=threads --concurrency=2`，是单进程多线程
- **扩展即冲突**：起第二个 worker 容器后，两个进程同时写同一个 `outputs/chroma_db/`，会锁冲突甚至数据损坏

这恰好和"引入 Celery 做水平扩展"的目标冲突。可选解法：

1. ChromaDB 切 `HttpClient` 模式（compose 里加 chroma 容器）
2. 知识库写入收敛到单独队列，只由一个 worker 消费
3. 换 pgvector（可复用已有 PostgreSQL）

---

## G. 其他待办（非 RAG，之前提过）

| 项 | 说明 |
|---|---|
| 前端接异步接口 | `/api/runs` + SSE 续传已做好但前端仍在用旧的长连接接口，断线续传能力没兑现 |
| requirements 版本漂移 | 写 `redis==5.2.1`，实际环境是 8.1.0（被 langgraph-checkpoint-redis 拉高） |
| inline 模式 `create_task` 未持引用 | Python 要求保存 Task 引用，理论上可能被 GC |
| `GET /safety/approvals` 不含图中的中断 | 刷新页面后待审批列表会空 |
| `backend/agents/graph.py` 33 行空桩 | 挪走 `rebuild_graph` 即可删 |
| Celery 每个任务都 `bootstrap()` | 重复 discover 技能，纯开销 |
| 多 worker 抢同一 run 无分布式锁 | 目前靠 `thread_id` 隔离，生产需 run 级锁 |

---

## 推荐执行顺序

1. **C1 + C2**（半小时，纯收益，无风险）
2. **E 评估集**（一天，后续所有优化的前提）
3. **A 策略学习 MVP**（2-3 小时，离线可跑，能出简历数字）
4. **B 检索层级化**（半天以上，改动大）
5. **D 混合检索 / rerank**（有评估基线之后再谈）
6. **F 架构风险**（真要水平扩展前必须解决）

---

## 面试相关提醒

- 策略学习写「上下文赌博机」，不写 MARL
- RAG 部分可以讲「两段式缓存判定 + 阈值实测标定」，这是真实且有含金量的
- 不要编造压测数字（QPS、延迟），见 `docs/project-pitch-interview.md` 第五节红旗问题
