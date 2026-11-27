"""Render saved customer-service regression evidence for sharing."""

from backend.tools.retrieval_report import _cell, _table


CHECK_LABELS = {
    "kind": "处理分支", "rule_id": "规则版本", "date": "规则核对日期", "text": "回复内容",
    "excerpt": "引用原文", "order_id": "订单号", "status": "售后状态", "first_kind": "第一轮处理",
    "private_data": "订单隔离", "checks": "待核实项",
}


def render_quality_report(report):
    rate = report["policy_recall_at_1"]
    recall = "不适用" if rate is None else f"{rate:.1%}"
    compared = report.get("comparison_run_id")
    can_show_delta = "comparison_run_id" not in report or compared is not None
    lines = [
        "# 客服回归评估报告", "",
        "本次评估使用模拟订单和问题，反映当前演示场景的回归结果。", "",
        *_table(["项目", "结果"], [
            ("运行编号", report["id"]),
            ("评估时间（UTC）", report["created_at"]),
            ("规则模拟日期", report.get("evaluation_date", "旧版未记录")),
            ("规则目录 SHA-256", report["catalog_sha256"]),
            ("用例 SHA-256", report.get("case_sha256", "旧版未记录")),
            ("模型配置", (report["intent_model"] or "本地规则") if "intent_model" in report else "旧版未记录"),
            ("上次可比评估编号", compared if compared else "无"),
            ("通过用例", f"{report['passed']} / {report['total']}"),
            ("规则引用命中率", recall),
            ("参与引用评估的问题数", report.get("policy_citation_queries", "旧版未记录")),
            ("较上次新增失败", ("、".join(report["regressed"]) or "无") if can_show_delta else "不适用"),
            ("较上次恢复通过", ("、".join(report["recovered"]) or "无") if can_show_delta else "不适用"),
        ]),
    ]
    if "baseline_passed" in report:
        lines += [f"同题旧流程通过 {report['baseline_passed']} / {report['total']}。"
                  f"新流程中模型返回有效分类 {report['model_turns']} 轮。", ""]
    for item in report["cases"]:
        result = "通过" if item["passed"] else "失败"
        lines += [f"## {_cell(item['id'])} · {_cell(item['category'])} · {result}", ""]
        if "baseline_replies" in item:
            lines += ["旧流程末轮回复：" + _cell(item["baseline_replies"][-1]["text"]), ""]
        if item["differences"]:
            differences = "、".join(CHECK_LABELS.get(key, key) for key in item["differences"])
            lines += [f"差异项 **{_cell(differences)}**", ""]
        lines += _table(["校验项目", "期望"], [
            (CHECK_LABELS.get(key, key), value) for key, value in item["expected"].items()
        ])
        turns = []
        for query, answer in zip(item["queries"], item["replies"]):
            citation = answer.get("citation") or {}
            turns.append((query, answer["kind"], answer["text"], citation.get("rule_id"),
                          citation.get("effective_date"), citation.get("excerpt")))
        lines += _table(["问题", "实际分支", "实际回复", "规则版本", "核对日期", "引用原文"], turns)
    return "\n".join(lines).rstrip() + "\n"
