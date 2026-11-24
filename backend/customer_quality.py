"""Deterministic support regression cases over the published demo catalog."""

import hashlib
import json

from backend.customer_service import DEMO_DATE, conversation_turn
from backend.tools.retrieval_eval import evaluate


def _current_rule(rules, topic, channel):
    today = DEMO_DATE.isoformat()
    return next((rule for rule in rules if (rule[1], rule[3], rule[4]) == (topic, channel, None)
                 and rule[5] <= today and (rule[6] is None or today < rule[6])), None)


def run_quality(rules):
    today = DEMO_DATE.isoformat()
    cases = [
        ("current-web", "政策版本", "a", ["当前活动有什么赠品？"], {"kind": "policy", "rule": _current_rule(rules, "gift", "web"), "date": today}),
        ("current-live", "政策版本", "b", ["直播间赠品门槛是什么？"], {"kind": "policy", "rule": _current_rule(rules, "gift", "live"), "date": today}),
        ("historical", "政策版本", "a", ["DEMO-1001 下单时有什么赠品规则？"], {"kind": "policy", "rule_id": "gift-nov-old", "text": "299 元", "date": "2026-11-02"}),
        ("clarify-order", "多轮对话", "a", ["什么时候发货？", "DEMO-1002"], {"kind": "order", "order_id": "DEMO-1002", "text": "待发货", "first_kind": "clarify"}),
        ("follow-up", "多轮对话", "b", ["DEMO-2001 物流到哪了？", "那能退吗？"], {"kind": "policy", "rule": _current_rule(rules, "returns", "live"), "date": today, "first_kind": "order"}),
        ("foreign-order", "订单隔离", "a", ["DEMO-2001 的物流"], {"kind": "unavailable"}),
        ("foreign-history", "订单隔离", "a", ["DEMO-2001 下单时的赠品规则"], {"kind": "unavailable"}),
        ("handoff", "人工交接", "a", ["使用后感觉不适，想转人工"], {"kind": "handoff"}),
        ("unknown", "无匹配询问", "a", ["你们的实体店在哪里？"], {"kind": "clarify"}),
        ("after-sale", "售后追踪", "a", ["DEMO-1001 售后进度"], {"kind": "after_sale", "status": "reviewing", "text": "人工审核中"}),
    ]
    results, rankings = [], []
    for identifier, category, buyer, queries, checks in cases:
        state, replies = {}, []
        after_sales = {"DEMO-1001": {"status": "reviewing"}} if identifier == "after-sale" else {}
        for query in queries:
            response, state = conversation_turn(buyer, query, "cream", "web", state, rules, after_sales)
            replies.append(response)
        actual = replies[-1]
        citation = actual.get("citation", {})
        expected = {key: value for key, value in checks.items() if key != "rule"}
        if "rule" in checks:
            rule = checks["rule"]
            if rule:
                expected.update(rule_id=rule[0], text=rule[7])
            else:
                expected.pop("date", None)
                expected["kind"] = "handoff"
        findings = []
        actual_fields = {"kind": actual["kind"], "rule_id": citation.get("rule_id"), "date": citation.get("effective_date"),
                         "order_id": actual.get("order", {}).get("id"), "first_kind": replies[0]["kind"],
                         "status": (actual.get("after_sale") or {}).get("status")}
        for key, value in expected.items():
            ok = value in actual["text"] if key == "text" else actual_fields[key] == value
            if not ok:
                findings.append(key)
        if expected["kind"] == "unavailable" and ("order" in actual or "citation" in actual):
            findings.append("private_data")
        if "rule_id" in expected:
            rankings.append({"query": identifier, "relevant_ids": [expected["rule_id"]],
                             "ranked_ids": [citation["rule_id"]] if citation.get("rule_id") else []})
        results.append({"id": identifier, "category": category, "queries": queries, "expected": expected,
                        "replies": replies, "passed": not findings, "differences": findings})
    fingerprint = hashlib.sha256(json.dumps(rules, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    citation_metrics = evaluate(rankings, k=1)
    return {"total": len(results), "passed": sum(item["passed"] for item in results), "cases": results,
            "policy_recall_at_1": citation_metrics["recall@1"], "catalog_sha256": fingerprint}
