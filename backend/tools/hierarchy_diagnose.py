"""Diagnose a reviewed label set against its pinned offline lexical corpus."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3

from backend.hierarchy import HierarchyStore
from backend.tools.hierarchy_labels import validate_annotations
from backend.tools.retrieval_io import read_json_input


STAGES = ("no_query_terms", "section_no_match", "section_pruned", "chunk_no_match", "below_top_k", "returned")
STAGE_NAMES = {"no_query_terms": "查询没有可用词项", "section_no_match": "章节没有匹配词项",
               "section_pruned": "章节排在候选范围外", "chunk_no_match": "所选路由未匹配分块",
               "below_top_k": "分块排在Top K之外", "returned": "已返回"}


def diagnose_labels(package, view, top_k=5, section_k=5, *, route="rrf"):
    rows = validate_annotations(package, view)["rows"]
    identity = view.retrieval_identity(top_k, section_k, route=route)
    details = []
    for row in rows:
        trace = view.relevance_trace(row["query"], row["relevant_ids"], top_k, section_k, route=route)
        details.append({"query": row["query"], "tags": row["tags"], "answerable": row["answerable"],
                        "ranked_ids": [hit["id"] for hit in trace["hits"]],
                        "relevance_trace": trace["relevance_trace"], "diagnostics": trace["diagnostics"]})

    def aggregate(items):
        counts = Counter(hit["stage"] for item in items for hit in item["relevance_trace"])
        return {"queries": len(items), "answerable_queries": sum(item["answerable"] for item in items),
                "no_answer_queries": sum(not item["answerable"] for item in items),
                "no_answer_nonempty_queries": sum(not item["answerable"] and bool(item["ranked_ids"]) for item in items),
                "relevant_chunks": sum(counts.values()), "stages": {stage: counts[stage] for stage in STAGES}}

    return {"kind": "hierarchy-diagnosis-v1", **identity, **aggregate(details), "details": details,
            "slices": {tag: aggregate([item for item in details if tag in item["tags"]])
                       for tag in sorted({tag for item in details for tag in item["tags"]})}}


def render_diagnosis(report):
    from backend.tools.retrieval_report import _cell, _table
    config = report["configuration"]
    lines = ["# 层级检索诊断", "", f"问题数：{report['queries']}；路由：{config['route']}；Top K：{config['top_k']}；章节候选：{config['section_k']}。", "",
             "统计单位是每题的相关分块；同一分块可在不同问题重复计数。阶段描述检索路径，不自动判断业务原因。", "",
             "语料指纹：" + report["corpus_sha256"], "", "检索指纹：" + report["retrieval_sha256"], "",
             "快照：" + "；".join(report["snapshot_ids"]), ""]
    if "input" in report:
        lines += ["标注文件：" + _cell(report["input"]["path"]), "", "标注指纹：" + report["input"]["sha256"], ""]
    for name, scope in [("整体", report), *report["slices"].items()]:
        lines += ["## " + _cell(name), "",
                  f"相关分块：{scope['relevant_chunks']}；无答案题：{scope['no_answer_queries']}；无答案但返回非空：{scope['no_answer_nonempty_queries']}。", ""]
        lines += _table(["阶段", "分块数"], [(STAGE_NAMES[stage], scope["stages"][stage]) for stage in STAGES])
    lines += ["## 逐题证据", ""]
    lines += _table(["问题", "相关分块", "阶段", "章节排名", "正文排名", "标题排名", "最终排名"],
                    [(item["query"], hit["id"], STAGE_NAMES[hit["stage"]], hit["section_rank"],
                      hit["body_rank"], hit["title_rank"], hit["rank"])
                     for item in report["details"] for hit in item["relevance_trace"]])
    lines += ["", "无答案题不产生相关分块追踪；没有词法匹配不代表语义上无关，仍需阅读原文与标注理由。"]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labels", type=Path)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--section-k", type=int, default=5)
    parser.add_argument("--route", choices=("rrf", "body"), default="rrf")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args()
    try:
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError("output already exists")
        if not args.output.parent.is_dir():
            raise FileNotFoundError("output directory does not exist")
        if not args.store.is_file():
            raise ValueError("store does not exist")
        package, source = read_json_input(args.labels)
        if not isinstance(package, dict):
            raise ValueError("labels must be a bound annotation package")
        view = HierarchyStore(args.store).prepare_versions(package.get("snapshot_ids"))
        report = diagnose_labels(package, view, args.top_k, args.section_k, route=args.route)
        report["input"] = source
        with args.output.open("x", encoding="utf-8") as handle:
            if args.format == "markdown":
                handle.write(render_diagnosis(report))
            else:
                json.dump(report, handle, ensure_ascii=False, indent=2)
        print(json.dumps({"output": str(args.output), "queries": report["queries"], "stages": report["stages"]}, ensure_ascii=False, indent=2))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
