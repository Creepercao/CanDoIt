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

导出器会检查知识库是否可用，但生产检索中的 embedding 不可用、底层查询失败也可能返回空列表。
应核对服务状态和日志，将失败批次排除或重新导出；空排名本身不能证明系统成功识别了无答案。

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

## 4. 设置整体和分组验收门槛

将业务确认的规则保存为 JSON 数组，例如 `gates.json`：

```json
[
  {"metric": "recall@5", "min_value": 0.8, "min_delta": 0},
  {"metric": "mrr@5", "slice": "售后", "min_delta": 0},
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

## 6. 无训练验证

```powershell
python -m pytest backend/tests/test_retrieval_eval.py backend/tests/test_retrieval_export.py backend/tests/test_retrieval_gates.py backend/tests/test_retrieval_report.py backend/tests/test_retrieval_cli.py -q
```

测试注入假的检索结果，覆盖混合标注、分组、截断指标、无答案集合、退步识别、验收门槛、
报告与输入指纹，以及独立进程运行评分 CLI 的文件链路；
不会加载模型、运行训练或访问真实检索服务。真实标注集与业务质量基线仍需另行建立。
