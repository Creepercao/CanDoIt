# 离线层级快照与上下文取回

这组工具承接 chunk_preview 的 JSON，保存到用户指定的独立 SQLite 文件。它不连接生产
ChromaDB、不生成向量、不训练模型。可调用离线词法检索得到命中分块，或由调用者提供分块 ID。
当前交付为分块准备后的本地快照、版本选择和父章节取回，尚未完成章节摘要与向量两级检索。

## 离线词法检索

完成导入和激活后，可输入问题并同时取回上下文：

```powershell
python -m backend.tools.hierarchy_store --store temp/hierarchy.sqlite search "退款规则" --section-k 5 --top-k 5 --with-context --max-chars 4000
```

检索只使用生效快照，先按章节路径与全文计算 BM25，保留前 section-k 个正分章节；
再在其下分块中分别计算正文 BM25 和标题路径 BM25，用 RRF 融合前 top-k 个结果。
每路候选在融合前保留全部正分结果，未命中词项的结果不会为凑数补齐。
标题路径路可召回正文不含查询词但标题相关的块。首阶段遗漏的章节无法由分块融合补回，
需要在评估时关注 section-k 的影响。

BM25 使用 k1=1.2、b=0.75 和正值 IDF；查询重复词只计算一次。英文标识符与数字转小写，
中文按单字及相邻双字切词，不跨标点组合。这是便于复现的分词基线，不包含词典分词、
同义词扩展、语义理解或医疗/业务规则。当前只覆盖基本中文汉字区与 ASCII 标识符。

RRF 使用每路 `1/(60+rank)` 相加，不混合 BM25 原始分数。相同路重复 ID 去重后排名，
同分按 ID 确定性排序。输出 score 是 RRF 分数，body_score/title_score/section_score 是
各阶段原始分数；ranks 的顺序为正文路、标题路。分数不是概率，不应套用向量相似度阈值。
当前两路均为词法检索，不属于向量与 BM25 的混合检索，也未使用 Cross-Encoder。

## 导出排名并评估

人工标注 JSON 沿用 retrieval_eval 的 query/relevant_ids/answerable/tags 格式，其中 relevant_ids
必须使用对应快照的分块 ID，不能直接将章节或旧知识库条目 ID 当作分块 ID：

```powershell
python -m backend.tools.hierarchy_export labels.json temp/hierarchy-rankings.json --store temp/hierarchy.sqlite --depth 20 --section-k 5
python -m backend.tools.retrieval_eval temp/hierarchy-rankings.json --k 5
```

hierarchy_export 仅调用本地词法检索，输出排名可直接用于已有基线对比、分组指标与退步题复核。
它拒绝无生效快照的库；检测到导出前后生效快照集合变化时中止，不创建排名文件。
输出路径必须不存在。标准输出另含输入标注指纹、检索配置和生效快照 ID，应随实验另行保留。
评估期间应停止其他进程切换版本：前后集合检查不是覆盖全部查询的数据库一致性事务，
无法保证检测到中途切换后又恢复的情况。标注相关性仍需人工确认，工具不验证标注 ID 的真实相关性。

对比须固定语料版本、分块参数、分词/BM25/RRF 实现与导出深度。section-k 改变时候选集会变化，
候选子集内计算的 BM25 分数也会变化，不应跨实验直接比较原始分数。
此基线尚无真实业务质量结论，测试中的人工样本得分不代表生产效果。

## 操作流程

在仓库根目录执行，确保 temp 目录存在，输入 manual.md 为已有文档：

```powershell
New-Item -ItemType Directory -Force temp
python -m backend.tools.chunk_preview manual.md --source manual --max-chars 1000 --overlap-chars 100 --output temp/manual-chunks.json
python -m backend.tools.hierarchy_store --store temp/hierarchy.sqlite import temp/manual-chunks.json
python -m backend.tools.hierarchy_store --store temp/hierarchy.sqlite list --source manual
```

导入会重建并校验文档、章节、分块及统计，拒绝错误版本、父子引用、字符位置、内容和 ID。
文件来源信息 `input` 不进入快照内容。合法内容保存后打印 `snapshot_id`，相同内容重复保存幂等。
快照 ID 同时包含内容和分块配置；相同文档改用另一分块参数时保留为另一快照。
数据库父目录必须预先存在，工具不扫描或迁移已有生产数据。

导入默认不激活。用上一步得到的完整 ID 明确激活；示例中的占位字符串须替换：

```powershell
python -m backend.tools.hierarchy_store --store temp/hierarchy.sqlite activate snapshot-替换为实际ID
```

也可在导入时添加 `--activate` 一次完成保存和激活。保存与激活是两个事务，激活失败时已保存
快照仍可能存在。每个来源只能有一个生效快照，不影响其他来源；回退时重新激活旧 ID 即可，
旧内容不会删除。版本列表按来源和 ID 排序，不表示导入时间或新旧顺序。

将真实命中分块 ID 按排名保存为 JSON 字符串数组，例如 `temp/hit-ids.json`，再取回上下文：

```powershell
python -m backend.tools.hierarchy_store --store temp/hierarchy.sqlite --output temp/context.json context temp/hit-ids.json --max-chars 4000
python -m backend.tools.hierarchy_store --store temp/hierarchy.sqlite verify
```

所有命令默认打印 JSON；可在子命令之前指定 `--output` 另存报告，已有文件不会被覆盖。
`import`、`context` 输入使用严格 JSON 读取器，拒绝重复键和非有限数值。输入/存储错误返回退出码 2。
未知快照或不合法输入不会新建数据库；CLI 的读取与切换命令要求数据库已经存在。
库方法 list_versions 对不存在的数据库返回空列表，verify 报错。

## 上下文规则

- 只接受生效快照里的分块 ID，未知或未生效版本的 ID 会使整次请求失败，不静默忽略。
  相同内容和位置产生的相同分块 ID 可以跨分块配置复用，匹配的是当前生效快照。
- 重复分块 ID 去重，同一父章节只返回一次，章节顺序按第一次命中出现的顺序排列。
  章节下不同分块的重叠文本不会被简单重复拼接。
- `max_chars` 为正整数，限制返回章节正文的总字符数。超预算时截断最后一章或省略后续章节，
  返回 `truncated` 和 `omitted_sections`。这是字符预算，不含 JSON 元数据，也不是模型 token 预算。
- `start/end` 对应实际返回原文区间，`section_end` 是完整章节末尾。截断从章节开头取前缀，
  因此命中位置可能处于被截掉的尾部，应结合匹配 ID 和截断标记判断是否需要扩大预算。
- JSON 包含快照 ID、文档 ID、章节 ID、来源、路径、匹配分块 ID 和原文，方便追溯。
  `context` 是按给定命中结果取回父章节，未实现检索、重排、自动摘要或跨章节上下文选择。

## 完整性与规模边界

`verify` 检查 SQLite quick_check、外键、全部快照的重建结果和内容指纹，以及生效来源与快照
来源是否一致。正常读取快照也会核对指纹和数据库身份字段；损坏不会当作正常内容返回。
它检测数据不一致，不提供签名或防止攻击者重写整个数据库的保证，也不自动修复或删除内容。

此版本在上下文请求时读取生效快照并查找分块，校验时会重建分块，适合小规模离线验证；
词法索引在每次查询时临时构建，尚无持久化分块检索索引、生产并发性能验证、自动过期清理或访问权限层。快照正文存储于 SQLite，
实验数据应放在 temp 等本地目录，不要提交包含业务文本的数据库。

## 无训练测试

```powershell
python -m pytest backend/tests/test_chunking.py backend/tests/test_chunk_preview.py backend/tests/test_hierarchy.py backend/tests/test_hierarchy_cli.py backend/tests/test_lexical.py backend/tests/test_hierarchy_search.py backend/tests/test_hierarchy_export.py -q
```

测试验证内容校验、幂等保存、配置版本隔离、生效回退、原文位置、预算截断、旧版本拒绝、
数据库内容损坏检测，以及独立进程导入→重新打开→激活→上下文→完整性检查。
