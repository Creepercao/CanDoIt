"""Preview hierarchical document chunks locally, without models or a database."""

import argparse
import hashlib
import json
import re
from pathlib import Path

from backend.chunking import prepare_document


def _literal(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    longest = max((len(match[0]) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [fence + "text", text, fence, ""]


def render_markdown(result):
    stats = result["stats"]
    config = result["configuration"]
    lines = ["# 层级分块预览", "", "来源键：", "", *_literal(result["document"]["source"]),
             f"原文字符：{stats['source_chars']}；章节：{stats['sections']}；分块：{stats['chunks']}。",
             f"块上限：{config['max_chars']}；配置重叠：{config['overlap_chars']}；"
             f"实际重复字符：{stats['overlap_chars']}。", "",
             "位置为原文字符索引，区间左闭右开；重叠仅发生在同一章节内。此预览未写入知识库。", ""]
    if "input" in result:
        lines += ["原始文件 SHA-256：" + result["input"]["sha256"], ""]
    by_parent = {}
    for chunk in result["chunks"]:
        by_parent.setdefault(chunk["parent_id"], []).append(chunk)
    for index, section in enumerate(result["sections"], 1):
        lines += [f"## 章节 {index}", "", "标题路径（无标题时为空）：", "",
                  *_literal(" / ".join(section["path"])),
                  f"章节 ID：{section['id']}；范围 [{section['start']}, {section['end']})。", ""]
        for number, chunk in enumerate(by_parent.get(section["id"], []), 1):
            lines += [f"### 分块 {number}", "",
                      f"ID：{chunk['id']}；范围 [{chunk['start']}, {chunk['end']})；字符数：{len(chunk['text'])}。", "",
                      *_literal(chunk["text"])]
    return "\n".join(lines).rstrip("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="UTF-8 Markdown or text file")
    parser.add_argument("--source", required=True, help="Stable logical document key")
    parser.add_argument("--max-chars", type=int, default=1000)
    parser.add_argument("--overlap-chars", type=int, default=0)
    parser.add_argument("--output", type=Path, help="Also save report; path must not exist")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args()
    try:
        raw = args.input.read_bytes()
        result = prepare_document(raw.decode("utf-8-sig"), args.source, args.max_chars, args.overlap_chars)
        result["input"] = {"path": str(args.input.resolve()), "bytes": len(raw),
                           "sha256": hashlib.sha256(raw).hexdigest()}
        report = render_markdown(result) if args.format == "markdown" else json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(report + "\n")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(report)


if __name__ == "__main__":
    main()
