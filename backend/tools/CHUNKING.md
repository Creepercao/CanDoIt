# 层级分块准备与预览

此工具是检索层级化的入库前准备：读取一份 UTF-8 Markdown 或纯文本，生成文档、章节、
分块三类记录。全程使用标准库，不训练、不调用 embedding、不连接或修改 ChromaDB。
当前生产 KnowledgeBase.add/search 仍沿用原有结构；尚未实现章节摘要、两级检索或旧数据迁移。

## 使用

在仓库根目录执行，输入是已经存在的文件，输出路径必须不存在：

```powershell
python -m backend.tools.chunk_preview manual.md --source product-manual --max-chars 1000 --overlap-chars 100 --output chunks.json
python -m backend.tools.chunk_preview manual.md --source product-manual --max-chars 1000 --overlap-chars 100 --format markdown --output chunks-preview.md
```

默认在标准输出打印 JSON；`--output` 另外保存相同内容的 UTF-8 文件，拒绝覆盖。
`--format markdown` 展示各章节路径、每块全文、字符位置、ID 和大小统计。
输入或参数错误返回退出码 2，不创建报告。空文档或纯空白文档不用于建索引。

`--source` 必须是稳定的逻辑来源键，例如 `product-manual`，不要随意使用随机器变化的临时路径。
同一来源与相同文本生成相同文档 ID，文本变化产生新文档版本；不同来源的相同文本具有不同 ID。
这不是增量更新协议，修改原文后不保证保留旧章节和分块 ID，后续入库仍须处理旧版本清理。

## 切分约定

- `max_chars` 为正整数，按 Python Unicode 字符数量限制，默认 1000；不是 token 上限，
  也不保证不会切开 emoji 组合字形。段落超过上限时按字符硬切，因此代码、表格也可能在块内拆开。
- 优先在空行段落结束处切分，无合适边界时硬切。原文尾部、空行、缩进和原始换行都保留在 JSON 中。
- `overlap_chars` 为整数，范围为 0 到 max_chars-1，默认 0。相邻块可重复指定数量字符，
  但绝不跨越章节；边界选择保证块持续前进，没有尾部重复循环。
- 章节识别支持行首最多三个空格后的 Markdown ATX 标题（1–6 个 `#`），保存标题层级路径。
  前言与重复标题分别保留，跳过 ``` 或 ~~~ 围栏内的标题，未关闭围栏一直延续至文末。
  这不是完整 CommonMark 解析器，不解析 Setext 标题、HTML 区块、列表中的标题或表格结构。
- 所有 `start/end` 都是去除 UTF-8 BOM 后原文的字符位置，左闭右开，非字节偏移。
  每个块的 `text` 等于 `document.text[start:end]`；重叠区域重复出现，所有块合起来完整覆盖原文。
  Markdown 预览仅将展示的换行统一为 LF，原始位置和精确内容请以 JSON 为准。

## 输出结构

| 字段 | 内容 |
| --- | --- |
| `version` / `configuration` | 准备格式版本和分块参数 |
| `document` | 来源键、稳定版本 ID、完整原文和去 BOM 文本的 SHA-256 |
| `sections` | 章节 ID、指向文档的 parent_id、原文位置、标题级别、路径和章节全文 |
| `chunks` | 分块 ID、指向章节的 parent_id、document_id、原文位置和分块全文 |
| `stats` | 原文字符数、章节数、分块数、分块字符总量及实际重复字符数 |
| `input` | CLI 实际读取的文件路径、原始字节数和 SHA-256（包含 BOM、保留换行） |

章节文本只属于该标题到下一个标题之间的范围；上级标题体不重复包裹下级章节全文。
路径记录语义层级，每个章节的 parent_id 则统一指向文档。分块 parent_id 才指向章节。
重复字符数 = 分块文本总字符数 - 原文字符数，不是不同字符的去重数量。

JSON 同时保留文档、章节和分块文本以便验证，因此体积可能大于原始文件；Markdown 预览也会包含
所有分块内容。这是单文件离线准备工具，尚无流式大文件处理、批量语料索引或自动效果提升结论。

## 验证

```powershell
python -m pytest backend/tests/test_chunking.py backend/tests/test_chunk_preview.py -q
```

测试检查长文尾部、段落边界、重叠重建、代码围栏、重复标题、父子关系、精确位置、UTF-8 BOM、
Windows 换行、CLI 独立进程运行及已有文件保护，均不需要模型或数据库。
