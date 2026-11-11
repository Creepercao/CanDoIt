# 检索评估操作说明

这组工具用于检查知识库的排名质量，不训练模型。`retrieval_eval` 只读本地 JSON；
`retrieval_export` 会访问项目配置的知识库及 embedding 服务，按正常检索链路导出排名。

## 1. 准备人工标注

下面仅演示格式，不代表当前知识库已有这些条目。将示例 ID 替换为实际知识库条目 ID。

```json
[
  {"query": "已收录问题", "relevant_ids": ["actual-entry-id"]},
  {"query": "知识库尚未收录的问题", "answerable": false, "relevant_ids": []}
]
```

- 默认 `answerable=true`，必须至少有一个相关条目 ID；可标多个相关条目。
- `answerable=false` 必须显式填写且相关 ID 列表必须为空。它表示人工确认在指定知识库
  快照内没有相关条目，不能仅因为某次检索为空就这样标注。
- 对比两组排名时，问题文本必须唯一，两组问题和相关 ID 集合必须一致，行顺序可不同。
  更新知识库或修改标注后，应重新导出对应基线。
- 记录知识库快照、embedding 配置、导出深度和阈值等实验条件。当前 JSON 报告不会自动
  保存这些条件；更换阈值时要同时检查有答案召回率与无答案空召回率，避免只靠提高阈值获得好看的空召回率。

## 2. 导出两组排名

在仓库根目录执行，阈值填写被评估配置的实际值，下面的 `0.3` 仅为示例：

```powershell
python -m backend.tools.retrieval_export labels.json baseline-rankings.json --depth 20 --threshold 0.3
# 应用待评估配置后，用同一份标注再次导出。
python -m backend.tools.retrieval_export labels.json candidate-rankings.json --depth 20 --threshold 0.3
```

输出保留 `query`、`relevant_ids`、显式填写的 `answerable`，增加按相关性排序的 `ranked_ids`。
每份输出必须使用尚不存在的路径。有答案问题检索为空时，保留原来的相关 ID，不能改成无答案样本。

导出器会检查知识库是否可用，但生产检索中的 embedding 不可用、底层查询失败也可能返回空列表。
应核对服务状态和日志，将失败批次排除或重新导出；空排名本身不能证明系统成功识别了无答案。

## 3. 评分与保存对比报告

```powershell
python -m backend.tools.retrieval_eval candidate-rankings.json --k 5
python -m backend.tools.retrieval_eval candidate-rankings.json --k 5 --baseline baseline-rankings.json --output comparison.json
```

评分始终向标准输出打印 JSON；`--output` 另外保存同样的 UTF-8 JSON，并拒绝覆盖已有文件。
输入无效不会创建报告文件。

| 指标 | 计算范围与含义 |
| --- | --- |
| `recall@k` | 只统计有答案问题；逐题计算前 k 个唯一条目命中的相关 ID 比例，再求平均 |
| `hit_rate@k` | 只统计有答案问题；前 k 个唯一条目至少命中一个相关 ID 的问题占比 |
| `mrr` | 只统计有答案问题；首个相关条目排名的倒数均值，未命中计 0；使用完整输入排名，不受 k 截断，但受导出深度限制 |
| `no_answer_empty_rate` | 只统计无答案问题；排名为空的问题占比，越高表示越少召回无关条目 |
| `no_answer_retrieval_rate` | 只统计无答案问题；排名非空的问题占比，是前一指标的补数 |

重复检索 ID 只算一次且不占额外排名。某类样本数为 0 时，该类指标及其增量为 `null`，
不能当作 0 分。无答案空召回率衡量的是检索阶段，不代表最终机器人拒答质量。

对比报告的 `delta` 为候选减基线。`query_changes` 按基线问题顺序列出逐题
`recall_delta`、`hit_delta`、`reciprocal_rank_delta`，以及仅用于无答案问题的
`empty_retrieval_delta`。不适用的字段为 `null`。召回率上升可能同时伴随首个相关结果后移，
需要结合两份 `details` 查看具体问题，不能仅凭单项增量判断整体改善。

## 4. 无训练验证

```powershell
python -m pytest backend/tests/test_retrieval_eval.py backend/tests/test_retrieval_export.py -q
```

测试注入假的检索结果，覆盖混合标注、无答案集合、逐题对比和两个命令行工具的文件链路；
不会加载模型、运行训练或访问真实检索服务。真实标注集与业务质量基线仍需另行建立。
