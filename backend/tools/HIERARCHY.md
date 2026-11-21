# 离线层级快照与上下文取回

这组工具承接 chunk_preview 的 JSON，保存到用户指定的独立 SQLite 文件。它不连接生产
ChromaDB、不生成向量、不训练模型。可调用离线词法检索得到命中分块，或由调用者提供分块 ID。
当前交付为分块准备后的本地快照、版本选择和父章节取回，尚未完成章节摘要与向量两级检索。

## 离线词法检索

完成导入和激活后，可输入问题并同时取回上下文：

```powershell
python -m backend.tools.hierarchy_store --store temp/hierarchy.sqlite search "退款规则" --section-k 5 --top-k 5 --with-context --max-chars 4000
python -m backend.tools.hierarchy_store --store temp/hierarchy.sqlite search "退款规则" --source manual --with-context --context-mode hit --max-chars 1000
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

### 策略对照与诊断

`search` 与 `hierarchy_export` 支持 `--route rrf|body`，默认 rrf 保留正文和标题路径两路融合。
body 保持相同的章节筛选，但只按分块正文 BM25 排名，便于评估标题路径路的影响。
body 模式的 score_kind 为 bm25、title_score 为 0，ranks 第二项为空；rrf 模式的 score_kind
为 rrf，两类原始分数不能直接比较。比较质量应使用同标注集的 Recall@K、MRR 等指标。

```powershell
python -m backend.tools.hierarchy_store --store temp/hierarchy.sqlite search "退款规则" --route body --with-context
python -m backend.tools.hierarchy_export labels.json temp/body.json --store temp/hierarchy.sqlite --route body
python -m backend.tools.hierarchy_export labels.json temp/rrf.json --store temp/hierarchy.sqlite --route rrf
python -m backend.tools.retrieval_eval temp/rrf.json --baseline temp/body.json --k 5
```

查询报告的 diagnostics 包含语料章节/分块数、分词结果、首阶段选中章节及分数、候选分块数、
各路正分候选数量、融合去重数量和最终结果数。candidate_cache_hit 只表示候选索引被复用，
不表示答案或查询结果命中缓存。无结果也会保留诊断信息，方便区分未命中章节与候选不足。

corpus_sha256 根据排序后的快照 ID 生成；retrieval_sha256 同时包含实际语料、显式检索版本、
分词标识、BM25/RRF 参数、route、top_k 和 section_k。指纹不包含查询文本、人工标签或缓存状态，
也不是代码字节或 Git 提交的哈希。查询变化、缓存淘汰不会改变它，配置和快照变化则会改变它。
检索实现变更时仍需维护版本号。还应保留输入标注指纹与实际排名，不能只凭配置指纹判断实验完全相同。

导出文件仍为原有排名数组；标准输出另含上述配置指纹、实际快照、来源范围与输入标注指纹，
并增加 rankings_sha256 对应实际写出的排名文件字节。应随实验保留标准输出元数据。

`search`、`context` 和 `hierarchy_export` 均支持重复填写 `--source`，将语料限定为指定来源的
生效快照。筛选发生在 BM25 统计与排名之前，因此分数也随语料范围变化；指定不存在或
没有生效快照的来源会报错。省略时使用全部生效来源。库 API 的 sources 接受非空字符串数组，
重复值去重，空数组不表示“全部”。从范围外取回上下文会报错，不会跨来源补齐结果。

## 固定版本与只读查询

读取数据库使用 SQLite mode=ro 连接，不建表、不写入、不自动初始化不相关数据库。
保存和激活使用明确的写连接。生效来源与快照正文通过同一条 SELECT 读取并验证身份，
缺失引用或来源不一致会直接报错，而非静默当作空语料。

可使用 `view = store.prepare(sources=["manual"])` 捕获固定语料，再调用 view.search、view.search_report 和
view.context。该视图脱离数据库，后续激活或回退不会影响它；active_snapshots 返回副本，
修改副本也不会改变视图。新建视图才反映新版本。视图构造时校验一次快照，仍占用完整正文内存，
不是持久索引或常驻服务缓存。查找表和章节 BM25 索引在准备时生成；上下文与查询不再逐次反序列化全文。
结果中的路径等可变字段返回副本，修改结果不会污染视图。

CLI `search --with-context` 的检索和章节取回共用一个视图，输出 snapshot_ids 可追溯实际版本。
单独调用 store.search/store.context 则各自捕获当时版本；需要多步一致性时应显式复用视图。

## 导出排名并评估

可先生成语料清单，查看实际章节与分块 ID，避免手工抄错：

```powershell
python -m backend.tools.hierarchy_store --store temp/hierarchy.sqlite --output temp/inventory.json inventory --source manual --preview-chars 160
```

清单包含文档来源、快照、父子 ID、原文位置和预览截断标记。需要逐块核对全文时添加
`--full-text`；content_preview 始终是预览，text 才是可选完整内容。清单不会自动生成相关性标签。
多个重叠分块可能都有关，应由标注者判断，不能仅因出现在同一章节就将其全标为相关。

人工标注 JSON 沿用 retrieval_eval 的 query/relevant_ids/answerable/tags 格式，其中 relevant_ids
必须使用对应快照的分块 ID，不能直接将章节或旧知识库条目 ID 当作分块 ID：

```powershell
python -m backend.tools.hierarchy_export labels.json temp/hierarchy-rankings.json --store temp/hierarchy.sqlite --depth 20 --section-k 5
python -m backend.tools.retrieval_eval temp/hierarchy-rankings.json --k 5
```

hierarchy_export 仅调用本地词法检索，输出排名可直接用于已有基线对比、分组指标与退步题复核。
默认选择时它拒绝无生效快照的库；明确指定历史快照时不要求该版本当前生效。
整批查询共用开始时捕获的固定语料视图，不会在查询之间混入新版本。
检索前还会检查整份标注的 relevant_ids 是否属于选定语料中的真实分块，未知 ID、章节 ID、
其他来源或其他版本的分块会报错，不把这种标注错误当成漏召回。此检查不验证语义相关性。
输出路径必须不存在。标准输出另含输入标注指纹、检索配置和生效快照 ID，应随实验另行保留。
其他进程切换生效版本不会改变已捕获的评估视图。active_snapshot_ids 记录本批实际使用的版本，
不保证这些版本在导出结束时仍是数据库最新生效版本。标注相关性仍需人工确认，工具不验证标注 ID 的真实相关性。
可追加 `--source manual` 导出指定来源；标注也应匹配该范围，避免把范围外标签当作召回失败。

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

### 停用与历史复现

```powershell
python -m backend.tools.hierarchy_store --store temp/hierarchy.sqlite deactivate manual
python -m backend.tools.hierarchy_store --store temp/hierarchy.sqlite list --source manual
```

停用只移除该来源的生效指针，保留所有历史快照。重复停用返回 deactivated=false。
新准备的默认视图不再包含该来源；已有固定视图仍可继续使用此前捕获的版本。重新 activate
可恢复该来源，但历史评估无需修改当前生效状态，直接指定快照即可：

```powershell
python -m backend.tools.hierarchy_store --store temp/hierarchy.sqlite search "退款规则" --snapshot-id snapshot-替换为实际ID --with-context
python -m backend.tools.hierarchy_store --store temp/hierarchy.sqlite --output temp/old-inventory.json inventory --snapshot-id snapshot-替换为实际ID --full-text
python -m backend.tools.hierarchy_export old-labels.json temp/old-rankings.json --store temp/hierarchy.sqlite --snapshot-id snapshot-替换为实际ID
```

search、context、inventory、hierarchy_export 的 `--snapshot-id` 可重复填写，且与 `--source`
互斥。同一来源只可选一个快照，防止把不同版本混成同一评估语料；重复相同 ID 去重，缺失 ID 报错。
库接口使用 `store.prepare_versions([...])`。快照正文与身份经过校验，查询不修改数据库或生效指针。

命令结果的 selection 为 active 或 snapshots，snapshot_ids/selected_snapshot_ids 记录实际版本。
导出中的 active_snapshot_ids 兼容字段只在默认生效版本选择时输出，指定历史版本时不会用它
冒充当前生效版本。历史标注也必须使用该历史快照的真实分块 ID。

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

- 默认只接受生效快照里的分块 ID；明确使用 --snapshot-id 时，接受指定快照中的分块。
  不属于本次选择范围的 ID 会使整次请求失败，不静默忽略。
  相同内容和位置产生的相同分块 ID 可以跨分块配置复用，匹配的是当前生效快照。
- 重复分块 ID 去重，同一父章节只返回一次，章节顺序按第一次命中出现的顺序排列。
  章节下不同分块的重叠文本不会被简单重复拼接。
- `max_chars` 为正整数，限制返回章节正文的总字符数。超预算时截断最后一章或省略后续章节，
  返回 `truncated` 和 `omitted_sections`。这是字符预算，不含 JSON 元数据，也不是模型 token 预算。
- `start/end` 对应实际返回原文区间，`section_start/section_end` 是完整章节范围。
  默认 `--context-mode prefix` 从章节开头取前缀，可能截掉尾部命中；`--context-mode hit`
  则优先围绕该章节的首个（排名最高）命中分块取窗口，预算够时保留整个首个分块并扩展邻近文本。
  预算比该分块还小时仅显示其前一部分，不能保证其他命中也都进入窗口。
- `matched_ranges` 列出所有匹配分块的原文区间；visible_chunk_ids 表示与窗口有交集，
  complete_chunk_ids 表示整块落入窗口。仅匹配不代表已完整展示，需同时查看截断与可见性字段。
- JSON 包含快照 ID、文档 ID、章节 ID、来源、路径、匹配分块 ID 和原文，方便追溯。
  `context` 是按给定命中结果取回父章节，未实现检索、重排、自动摘要或跨章节上下文选择。

## 完整性与规模边界

`verify` 检查 SQLite quick_check、外键、全部快照的重建结果和内容指纹，以及生效来源与快照
来源是否一致。正常读取快照也会核对指纹和数据库身份字段；损坏不会当作正常内容返回。
它检测数据不一致，不提供签名或防止攻击者重写整个数据库的保证，也不自动修复或删除内容。

此版本在上下文请求时读取生效快照并查找分块，校验时会重建分块，适合小规模离线验证；
章节索引随固定视图复用；候选章节集合的正文与标题索引按 LRU 最多保留 4 组，超过后淘汰，
重新构建不改变语料统计和排名。这只限制索引组数，不是字节内存上限，大语料仍应注意整体占用。
每次 store.search 都会新建视图；批量查询应复用 prepare 返回的视图以获得复用收益。
尚无持久化分块检索索引、生产并发性能验证、自动过期清理或访问权限层。快照正文存储于 SQLite，
实验数据应放在 temp 等本地目录，不要提交包含业务文本的数据库。

## 无训练测试

标注草稿 API `hierarchy_labels.draft_annotations(questions, view)` 接受包含 query 和可选 tags
的问题列表，记录固定视图的 snapshot_ids 和 corpus_sha256。草稿中的 answerable=null、
reviewed=false 表示待人工判断，不能把空相关 ID 自动视为“知识库没有答案”。草稿不提供
检索排名或自动相关性标签，避免将检索器自己的结果直接当成评估真值。

```powershell
python -m pytest backend/tests/test_chunking.py backend/tests/test_chunk_preview.py backend/tests/test_hierarchy.py backend/tests/test_hierarchy_inventory.py backend/tests/test_hierarchy_cli.py backend/tests/test_lexical.py backend/tests/test_hierarchy_search.py backend/tests/test_hierarchy_export.py -q
```

测试验证内容校验、幂等保存、配置版本隔离、生效回退、原文位置、预算截断、旧版本拒绝、
数据库内容损坏检测，以及独立进程导入→重新打开→激活→上下文→完整性检查。
