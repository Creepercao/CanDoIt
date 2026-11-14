# 检索评估操作说明

这组工具用于检查知识库的排名质量，不训练模型。`retrieval_eval` 只读本地 JSON；
`retrieval_export` 会访问项目配置的知识库及 embedding 服务，按正常检索链路导出排名。

## 1. 准备人工标注

下面仅演示格式，不代表当前知识库已有这些条目。将示例 ID 替换为实际知识库条目 ID。

```json
[
  {"query": "已收录问题", "relevant_ids": ["actual-entry-id"], "tags": ["售后", "长尾"]},
  {"query": "知识库尚未收录的问题", "answerable": false, "relevant_ids": []}
]
```

- 默认 `answerable=true`，必须至少有一个相关条目 ID；可标多个相关条目。
- `answerable=false` 必须显式填写且相关 ID 列表必须为空。它表示人工确认在指定知识库
  快照内没有相关条目，不能仅因为某次检索为空就这样标注。
- 可选 `tags` 为非空字符串数组，表示业务分组。一个问题可属于多个分组；重复标签只计一次。
  不填或填空数组时仍参与整体指标，不参与任何标签分组。分组可能重叠，不能相加当作总问题数。
- 对比两组排名时，问题文本必须唯一，两组问题、相关 ID 集合及标签集合必须一致，行顺序可不同。
  更新知识库或修改标注后，应重新导出对应基线。
- 记录知识库快照、embedding 配置、导出深度和阈值等实验条件。评分 CLI 会保存所读取
  排名和验收规则的文件路径、SHA-256 与字节数，但不会自动保存这些服务端实验条件。
  更换阈值时要同时检查有答案召回率与无答案空召回率，避免只靠提高阈值获得好看的空召回率。

## 2. 导出两组排名

在仓库根目录执行，阈值填写被评估配置的实际值，下面的 `0.3` 仅为示例：

```powershell
python -m backend.tools.retrieval_export labels.json baseline-rankings.json --depth 20 --threshold 0.3
# 应用待评估配置后，用同一份标注再次导出。
python -m backend.tools.retrieval_export labels.json candidate-rankings.json --depth 20 --threshold 0.3
```

输出保留 `query`、`relevant_ids`、显式填写的 `answerable` 和 `tags`，增加按相关性排序的 `ranked_ids`。
每份输出必须使用尚不存在的路径。有答案问题检索为空时，保留原来的相关 ID，不能改成无答案样本。
导出会先校验完整标注、查询唯一性、深度、阈值、已有输出和父目录，再初始化知识库。
最终写入仍使用独占创建，防止检索期间出现同名文件时被覆盖。

导出器使用 `KnowledgeBase.search(..., strict=True)`。知识库不可用、初始化异常、embedding
不可用或请求异常、底层查询异常、缺失或非有限的距离会抛出 `KnowledgeSearchError`，
CLI 以退出码 2 终止，不创建排名文件。某题失败时，前面题目的临时结果也不会保存为完整批次。
正常查询后全部结果低于阈值仍是合法空排名，保留以便评分；它本身不代表最终机器人正确拒答。

普通业务调用默认 `strict=False`，服务不可用时继续降级为空列表，异常距离条目被跳过。
检索入口在初始化前检查非空查询、1–200 的整数 top_k 和 [-1, 1] 内有限阈值，
不会将非法参数作为正常空召回。严格模式只用于需要显式区分失败的调用，不会改变默认业务开关。

可用 `--timeout-seconds 60` 设置逐题等待超时（默认 60 秒，必须为有限正数）。超时后
报错提示失败行号，取消该题的异步等待，不执行后续问题，不写出本批排名。该限制是 asyncio
协作式超时，不能强制打断阻塞事件循环的同步数据库操作或不响应取消的代码；它也不是整批时间预算。

在实际导出前，可仅检查标注与配置：

```powershell
python -m backend.tools.retrieval_export labels.json --threshold 0.3 --validate-only
```

此模式不导入知识库、不连接服务，不写文件。标准输出为输入指纹、检索配置、
有答案/无答案数量及各分组样本数，不生成质量分数，也不验证相关 ID 是否存在于真实知识库。
可以附带输出路径来检查已存在文件和父目录；正常导出仍必须提供输出路径。

## 3. 评分与保存对比报告

```powershell
python -m backend.tools.retrieval_eval candidate-rankings.json --k 5
python -m backend.tools.retrieval_eval candidate-rankings.json --k 5 --baseline baseline-rankings.json --output comparison.json
python -m backend.tools.retrieval_eval candidate-rankings.json --k 5 --baseline baseline-rankings.json --format markdown --output comparison.md
```

默认向标准输出打印 JSON；`--format markdown` 生成含整体、分组和逐题指标的阅读报告。
`--output` 另外保存相同内容的 UTF-8 文件，并拒绝覆盖已有文件。输入无效不会创建报告文件。
输入支持普通 UTF-8 和带 BOM 的 UTF-8；文件指纹对应实际解析的原始字节。`evaluate` 和
`compare` 函数不读文件，只有 CLI 报告带 `inputs` 指纹。
评分、导出及复核三个 CLI 共用严格 JSON 读取器：拒绝任意层级的重复键、NaN、Infinity
以及溢出为无穷的数值，防止规则或标签被静默覆盖。非法输入返回退出码 2。

| 指标 | 计算范围与含义 |
| --- | --- |
| `recall@k` | 只统计有答案问题；逐题计算前 k 个唯一条目命中的相关 ID 比例，再求平均 |
| `hit_rate@k` | 只统计有答案问题；前 k 个唯一条目至少命中一个相关 ID 的问题占比 |
| `mrr` | 只统计有答案问题；首个相关条目排名的倒数均值，未命中计 0；使用完整输入排名，不受 k 截断，但受导出深度限制 |
| `mrr@k` | 只统计有答案问题；首个相关条目在前 k 个唯一结果内才计算排名倒数，否则计 0 |
| `no_answer_empty_rate` | 只统计无答案问题；排名为空的问题占比，越高表示越少召回无关条目 |
| `no_answer_retrieval_rate` | 只统计无答案问题；排名非空的问题占比，是前一指标的补数 |

重复检索 ID 只算一次且不占额外排名。某类样本数为 0 时，该类指标及其增量为 `null`，
不能当作 0 分。无答案空召回率衡量的是检索阶段，不代表最终机器人拒答质量。

对比报告的 `delta` 为候选减基线。`query_changes` 按基线问题顺序列出逐题
`recall_delta`、`hit_delta`、`reciprocal_rank_delta`，以及仅用于无答案问题的
`empty_retrieval_delta`。`reciprocal_rank_at_k_delta` 对应 MRR@K 的逐题增量。
不适用的字段为 `null`。召回率上升可能同时伴随首个相关结果后移，
需要结合两份 `details` 查看具体问题，不能仅凭单项增量判断整体改善。

`slices` 按标签独立计算样本数和指标，采用各自的有答案/无答案分母。`change_summary`
分别列出每项正向质量指标的 `improved`、`regressed`、`unchanged` 问题列表和可比较样本数。
未定义的指标不计为“不变”；不同指标间可能存在取舍，工具不会强行合并成一个总分。
逐题 `details` 还保留相关 ID、去重后的完整排名、前 K 命中的 `hit_ids` 和未命中的
`missed_ids`。出现在 K 之后的相关条目也属于前 K 漏检，不能当作整个排名中不存在。
Markdown 报告的“命中与漏检定位”展示这些条目，方便回查召回与重排环节。

## 4. 设置整体和分组验收门槛

将业务确认的规则保存为 JSON 数组，例如 `gates.json`：

```json
[
  {"metric": "recall@5", "min_value": 0.8, "min_delta": 0},
  {"metric": "mrr@5", "slice": "售后", "min_delta": 0, "min_queries": 20, "max_regression_rate": 0.05},
  {"metric": "no_answer_empty_rate", "min_value": 0.9}
]
```

```powershell
python -m backend.tools.retrieval_eval candidate-rankings.json --baseline baseline-rankings.json --gates gates.json --output checked-comparison.json
```

- `min_value` 是候选指标最低分，范围 [0, 1]；`min_delta` 是候选减基线的最低增量，范围 [-1, 1]。
  允许同时设置，两项必须全部满足。浮点比较使用 1e-12 的绝对容差。
- `slice` 可省略，省略时检查整体；填写时必须为现有标签。指标中的 K 必须与 `--k` 一致。
  所有规则都采用越高越好的方向，因此不接受 `no_answer_retrieval_rate`，应改查其补数
  `no_answer_empty_rate`。示例阈值只是格式演示，不能直接当作实际业务标准。
- 缺少对应类型样本导致指标为 `null` 时，规则以 `undefined_metric` 判定未通过，不能将其当作零分通过验收。
- 可选 `min_queries` 为正整数，检查所选范围内该指标真正使用的样本数：无答案空召回率
  统计无答案问题，其余指标统计有答案问题。未达到要求时返回 `insufficient_samples`，
  报告保留 `sample_count`。例如 2 道有答案和 10 道无答案问题，MRR 的样本量仍然只有 2。
- 可选 `max_regression_rate` 为 [0, 1]，限制该指标逐题退步的比例。分母与指标样本数一致，
  选择分组时只计算该组；不满足时返回 `excessive_regressions`。平均分持平也可能触发此限制。
  所有配置约束都须满足，可只设置样本量或退步比例；这类门槛仍要求指标有定义。
- 使用 `--gates` 必须提供 `--baseline`。退出码 **0** 表示正常评分或全部门槛通过；**1**
  表示门槛未通过，报告仍会打印并保存；**2** 表示输入、规则或输出路径错误，不保存新报告。
- 整体平均分通过不保证每个问题都变好。用关键业务分组门槛和退步题列表补充人工检查，
  这些门槛不是显著性检验，也不代替对服务失败批次的检查。

## 5. 可直接运行的离线演示

仓库 `backend/tools/examples/retrieval-*.json` 是三道**人工构造**的问题排名，不读取真实知识库，
也不代表本项目业务效果。可在仓库根目录直接运行：

```powershell
python -m backend.tools.retrieval_eval backend/tools/examples/retrieval-candidate.json --baseline backend/tools/examples/retrieval-baseline.json --gates backend/tools/examples/retrieval-gates.json --format markdown --output demo-comparison.md
```

这份演示刻意展示：整体 Recall@5 从 0.75 升至 1，整体 MRR@5 保持 0.75，
但“多意图”分组的 MRR@5 从 1 降至 0.5。分组门槛应未通过并返回退出码 **1**，报告仍生成。
输出路径必须不存在；生成的演示报告是本地文件，无需加入 Git。

## 6. 导出退步题复核材料

```powershell
python -m backend.tools.retrieval_review backend/tools/examples/retrieval-candidate.json --baseline backend/tools/examples/retrieval-baseline.json --metric mrr@5 --slice 多意图 --output review.json
```

不填写 `--metric` 时选择任一正向指标退步的问题；不填写 `--slice` 时检查全部问题。
指标中的 K 必须匹配 `--k`（默认 5）。只按退步筛选，不将召回变好但首条后移的题整体判为改善。
输出保留双方完整排名、相关 ID、命中和漏检、逐题指标变化、退步指标列表、输入文件指纹，
并提供空白 `review_note` 供人工填写。空备注不代表已复核，也不会自动生成新的相关性标签。
没有退步时正常生成空 `cases`；错误指标或分组返回退出码 2。输出路径必须不存在，
重复执行不会覆盖已经填写的备注。查看条目原文与检索服务日志后，再判断是标签、召回还是重排问题。

## 7. 无训练验证

```powershell
python -m pytest backend/tests/test_retrieval_eval.py backend/tests/test_retrieval_export.py backend/tests/test_retrieval_gates.py backend/tests/test_retrieval_report.py backend/tests/test_retrieval_cli.py backend/tests/test_retrieval_review.py backend/tests/test_retrieval_io.py -q
```

测试注入假的检索结果，覆盖混合标注、分组、截断指标、无答案集合、退步识别、验收门槛、
报告与输入指纹，以及独立进程运行评分 CLI 的文件链路；
不会加载模型、运行训练或访问真实检索服务。真实标注集与业务质量基线仍需另行建立。
