# 离线层级快照与上下文取回

这组工具承接 chunk_preview 的 JSON，保存到用户指定的独立 SQLite 文件。它不连接生产
ChromaDB、不生成向量、不训练模型，也不判断哪些分块与问题相关；命中的分块 ID 由调用者提供。
当前交付为分块准备后的本地快照、版本选择和父章节取回，尚未完成章节摘要与向量两级检索。

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
尚无分块数据库索引、生产并发性能验证、自动过期清理或访问权限层。快照正文存储于 SQLite，
实验数据应放在 temp 等本地目录，不要提交包含业务文本的数据库。

## 无训练测试

```powershell
python -m pytest backend/tests/test_chunking.py backend/tests/test_chunk_preview.py backend/tests/test_hierarchy.py backend/tests/test_hierarchy_cli.py -q
```

测试验证内容校验、幂等保存、配置版本隔离、生效回退、原文位置、预算截断、旧版本拒绝、
数据库内容损坏检测，以及独立进程导入→重新打开→激活→上下文→完整性检查。
