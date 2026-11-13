"""Render dependency-free Markdown reports from retrieval evaluation results."""

import html
import re


def _cell(value):
    if value is None:
        return "不适用"
    if type(value) is float:
        return format(value, ".12g")
    text = html.escape(str(value)).replace("\\", "\\\\").replace("|", "&#124;")
    text = re.sub(r"([`*_\[\]])", r"\\\1", text)
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def _table(headers, rows):
    return ["| " + " | ".join(_cell(value) for value in headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *["| " + " | ".join(_cell(value) for value in row) + " |" for row in rows], ""]


def render_markdown(result):
    comparison = "candidate" in result
    score = result["candidate"] if comparison else result
    k = score["k"]
    metrics = (f"recall@{k}", f"hit_rate@{k}", f"mrr@{k}", "mrr",
               "no_answer_empty_rate", "no_answer_retrieval_rate")
    lines = ["# 检索评估报告", "",
             f"问题数：{score['queries']}；有答案：{score['answerable_queries']}；"
             f"无答案：{score['no_answer_queries']}；K：{k}。", "",
             "MRR 使用完整输入排名，MRR@K 只统计前 K 个唯一结果。不适用表示缺少对应类型的样本。",
             "no_answer_retrieval_rate 越低越好；其他指标越高越好。", ""]

    def metric_table(scope):
        if comparison:
            return _table(["指标", "基线", "候选", "候选减基线"],
                          [(key, scope["baseline"][key], scope["candidate"][key], scope["delta"][key])
                           for key in metrics])
        return _table(["指标", "得分"], [(key, scope[key]) for key in metrics])

    lines += ["## 整体指标", "", *metric_table(result)]
    if "inputs" in result:
        lines += ["## 输入文件指纹", "",
                  "指纹对应实际读取的文件字节，用于核对标注排名与验收规则；不代表知识库快照或模型配置。", ""]
        lines += _table(["用途", "路径", "SHA-256", "字节数"],
                        [(role, item["path"], item["sha256"], item["bytes"])
                         for role, item in result["inputs"].items()])
    for tag, scope in result.get("slices", {}).items():
        counts = scope["candidate"] if comparison else scope
        lines += ["## 业务分组：" + _cell(tag), "",
                  f"问题数：{counts['queries']}；有答案：{counts['answerable_queries']}；"
                  f"无答案：{counts['no_answer_queries']}。", "", *metric_table(scope)]
    if "gate_result" in result:
        gates = result["gate_result"]
        lines += ["## 验收门槛", "", "结果：" + ("通过" if gates["passed"] else "未通过"), ""]
        lines += _table(["指标", "范围", "最低样本量", "实际样本量", "最高退步比例", "实际退步比例", "最低候选分", "最低增量", "候选分", "实际增量", "结果", "原因"],
                        [(check["metric"], check.get("slice", "整体"), check.get("min_queries"), check["sample_count"],
                          check.get("max_regression_rate"), check["regression_rate"], check.get("min_value"),
                          check.get("min_delta"), check["candidate"], check["delta"],
                          "通过" if check["passed"] else "未通过", check["reason"])
                         for check in gates["checks"]])
    if comparison:
        lines += ["## 逐项改善与退步", ""]
        lines += _table(["指标", "可比较问题数", "改善数", "退步数", "不变数", "退步问题"],
                        [(metric, item["compared_queries"], len(item["improved"]),
                          len(item["regressed"]), len(item["unchanged"]), "；".join(item["regressed"]))
                         for metric, item in result["change_summary"].items()])
        lines += ["## 逐题变化", ""]
        lines += _table(["问题", "类型", "Recall变化", "命中变化", "MRR@K变化", "MRR变化", "无答案空召回变化"],
                        [(item["query"], "有答案" if item["answerable"] else "无答案",
                          item["recall_delta"], item["hit_delta"], item["reciprocal_rank_at_k_delta"],
                          item["reciprocal_rank_delta"], item["empty_retrieval_delta"])
                         for item in result["query_changes"]])
    else:
        lines += ["## 逐题得分", ""]
        lines += _table(["问题", "类型", "业务标签", "召回条数", "Recall", "MRR@K", "MRR"],
                        [(item["query"], "有答案" if item["answerable"] else "无答案",
                          "；".join(item["tags"]), item["retrieved_count"], item["recall"],
                          item["reciprocal_rank_at_k"], item["reciprocal_rank"])
                         for item in result["details"]])
    lines += ["## 命中与漏检定位", "", "命中和漏检均按前 K 个去重结果计算；排名保留完整输入深度。", ""]
    lines += _table(["问题", "相关ID", "前K命中ID", "前K漏检ID", "完整排名"],
                    [(item["query"], "；".join(item["relevant_ids"]), "；".join(item["hit_ids"]),
                      "；".join(item["missed_ids"]), "；".join(item["ranked_ids"]))
                     for item in score["details"]])
    return "\n".join(lines)
