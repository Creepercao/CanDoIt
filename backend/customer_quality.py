"""Deterministic support regression cases over the published demo catalog."""

import hashlib
import json
from collections import Counter
from datetime import date, timedelta

from backend.customer_intent import classify, configured_model
from backend.customer_service import DEMO_DATE, conversation_turn as baseline_turn, support_turn
from backend.tools.retrieval_eval import evaluate


HISTORICAL_GIFT_TEXT = "活动期间单笔实付满 299 元赠旅行装一份，以订单结算页显示为准。"
BOUNDARY_MARKERS = {
    "before_start": "生效前一天", "starts": "草稿生效日", "base_cutoff": "发布后上版停止适用",
    "base_expires": "现行版原定失效日", "last_active": "草稿最后有效日", "expires": "草稿失效日",
}


def _current_rule(rules, topic, channel, today, *, product=None):
    return next((rule for rule in rules if (rule[1], rule[3], rule[4]) == (topic, channel, product)
                 and rule[5] <= today and (rule[6] is None or today < rule[6])), None)


def conversation_turn(buyer, query, *args, as_of=DEMO_DATE):
    return support_turn(buyer, query, *args, classify(query), as_of=as_of)


def offline_turn(buyer, query, *args, as_of=DEMO_DATE):
    return support_turn(buyer, query, *args, classify(query, model_name=""), as_of=as_of)


def run_quality(rules, *, offline=False, as_of=DEMO_DATE):
    today = as_of.isoformat()
    model_name = "" if offline else configured_model()
    cases = [
        ("current-web", "政策版本", "a", ["当前活动有什么赠品？"], {"kind": "policy", "rule": _current_rule(rules, "gift", "web", today), "date": today}),
        ("current-live", "政策版本", "b", ["直播间赠品门槛是什么？"], {"kind": "policy", "rule": _current_rule(rules, "gift", "live", today), "date": today}),
        ("historical", "政策版本", "a", ["DEMO-1001 下单时有什么赠品规则？"], {"kind": "policy", "rule_id": "gift-nov-old", "text": HISTORICAL_GIFT_TEXT, "date": "2026-11-02"}),
        ("clarify-order", "多轮对话", "a", ["什么时候发货？", "DEMO-1002"], {"kind": "order", "order_id": "DEMO-1002", "text": "待发货", "first_kind": "clarify"}),
        ("follow-up", "多轮对话", "b", ["DEMO-2001 物流到哪了？", "那能退吗？"], {"kind": "policy", "rule": _current_rule(rules, "returns", "live", today), "date": today, "first_kind": "order"}),
        ("foreign-order", "订单隔离", "a", ["DEMO-2001 的物流"], {"kind": "unavailable"}),
        ("foreign-history", "订单隔离", "a", ["DEMO-2001 下单时的赠品规则"], {"kind": "unavailable"}),
        ("handoff", "人工交接", "a", ["使用后感觉不适，想转人工"], {"kind": "handoff"}),
        ("unknown", "无匹配询问", "a", ["你们的实体店在哪里？"], {"kind": "clarify"}),
        ("after-sale", "售后追踪", "a", ["DEMO-1001 售后进度"], {"kind": "after_sale", "status": "reviewing", "text": "人工审核中"}),
    ]
    variants = [
        ["活动现在送啥？"], ["直播间买满多少送东西？"], ["DEMO-1001 买的时候满多少送东西？"],
        ["这单啥时候发货呀？", "DEMO-1002"], ["DEMO-2001 签收了吗？", "那可以退吗？"],
        ["帮忙看下 DEMO-2001 到哪了"], ["买的时候送不送小样 DEMO-2001"],
        ["能帮忙找人工看看吗？"], ["线下哪里有店呀？"], ["查查退货进度 DEMO-1001"],
    ]
    cases += [(key + "-spoken", category, buyer, variant, checks)
              for (key, category, buyer, _, checks), variant in zip(cases, variants, strict=True)]
    gift_checks = {"kind": "gift_review", "rule_id": "gift-nov-old", "date": "2026-11-02",
                   "order_id": "DEMO-1001", "excerpt": HISTORICAL_GIFT_TEXT,
                   "checks": ["订单实付金额", "赠品出库记录"],
                   "text": f"你下单时的规则是：{HISTORICAL_GIFT_TEXT} 当前记录缺少订单实付金额和赠品出库记录，还需人工核实。"}
    cases += [
        ("missing-gift", "赠品核实", "a", ["DEMO-1001 那盒旅行装怎么没跟包裹一起来？"], gift_checks),
        ("missing-followup", "赠品核实", "a", ["小样是不是漏发了？", "DEMO-1001"], {**gift_checks, "first_kind": "clarify"}),
        ("missing-foreign", "订单隔离", "a", ["DEMO-2001 的旅行装没收到"], {"kind": "unavailable"}),
        ("missing-mixed", "多项诉求", "a", ["DEMO-1001 物流到哪了？还有赠品是不是漏发了？"], {"kind": "clarify"}),
        ("returns-web", "政策版本", "a", ["品牌商城拆封后能退换吗？"],
         {"kind": "policy", "rule": _current_rule(rules, "returns", "web", today), "date": today}),
        ("product-cream", "商品规格", "a", ["舒缓面霜的净含量是多少？"],
         {"kind": "policy", "rule": _current_rule(rules, "product", None, today, product="cream"), "date": today}),
        ("product-cleanser", "商品规格", "a", ["氨基酸洁面的容量是多少？"],
         {"kind": "policy", "rule": _current_rule(rules, "product", None, today, product="cleanser"), "date": today}),
    ]
    results, rankings = _evaluate(cases, rules, offline_turn if offline else conversation_turn, as_of)
    baseline, _ = _evaluate(cases, rules, baseline_turn, as_of)
    for result, original in zip(results, baseline):
        result["baseline_passed"] = original["passed"]
        result["baseline_replies"] = original["replies"]
    fingerprint = hashlib.sha256(json.dumps(rules, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    case_spec = [(key, category, buyer, queries, {name: value for name, value in checks.items() if name != "rule"})
                 for key, category, buyer, queries, checks in cases]
    case_hash = hashlib.sha256(json.dumps(case_spec, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()
    sources = Counter(reply.get("intent_source", "unrecorded") for item in results for reply in item["replies"])
    citation_metrics = evaluate(rankings, k=1)
    return {"total": len(results), "passed": sum(item["passed"] for item in results), "cases": results,
            "baseline_passed": sum(item["passed"] for item in baseline),
            "model_turns": sources["model"], "intent_sources": dict(sources), "intent_model": model_name,
            "case_sha256": case_hash, "evaluation_date": today,
            "policy_recall_at_1": citation_metrics["recall@1"],
            "policy_citation_queries": citation_metrics["answerable_queries"], "catalog_sha256": fingerprint}


def draft_boundary_dates(base_end, cutoff, starts_at, ends_at):
    start = date.fromisoformat(starts_at)
    end = date.fromisoformat(ends_at) if ends_at else None
    one_day = timedelta(days=1)
    checkpoints = [
        (start - one_day, "before_start"), (start, "starts"),
        (date.fromisoformat(cutoff), "base_cutoff"),
        (date.fromisoformat(base_end) if base_end else None, "base_expires"),
        (end - one_day if end else None, "last_active"), (end, "expires"),
    ]
    dates = {}
    for day, marker in checkpoints:
        if day is not None:
            dates.setdefault(day, []).append(marker)
    return sorted(dates.items())


def review_draft_boundaries(current, proposed, draft_hash, window):
    checks = [{"markers": markers, **compare_draft_quality(current, proposed, as_of=day)}
              for day, markers in draft_boundary_dates(**window)]
    result = {"draft_sha256": draft_hash, "marker_labels": BOUNDARY_MARKERS, "checks": checks}
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {**result, "review_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest()}


def compare_draft_quality(current, proposed, *, as_of=DEMO_DATE):
    before, after = (run_quality(rules, offline=True, as_of=as_of) for rules in (current, proposed))
    changes = []
    for old, new in zip(before["cases"], after["cases"], strict=True):
        coverage_lost = old["expected"]["kind"] == "policy" and new["expected"]["kind"] == "handoff"
        if coverage_lost or old["replies"] != new["replies"] or old["passed"] != new["passed"]:
            changes.append({"id": old["id"], "queries": old["queries"], "current_replies": old["replies"],
                            "proposed_replies": new["replies"], "current_passed": old["passed"],
                            "proposed_passed": new["passed"], "coverage_lost": coverage_lost})
    return {"demo_date": DEMO_DATE.isoformat(), "evaluation_date": as_of.isoformat(),
            "total": before["total"], "current_passed": before["passed"],
            "proposed_passed": after["passed"], "changes": changes,
            "regressed": [item["id"] for item in changes if item["current_passed"] and not item["proposed_passed"]],
            "coverage_lost": [item["id"] for item in changes if item["coverage_lost"]],
            "current_catalog_sha256": before["catalog_sha256"], "proposed_catalog_sha256": after["catalog_sha256"]}


def _evaluate(cases, rules, turn, as_of):
    results, rankings = [], []
    for identifier, category, buyer, queries, checks in cases:
        state, replies = {}, []
        after_sales = {"DEMO-1001": {"status": "reviewing"}} if category == "售后追踪" else {}
        for query in queries:
            response, state = turn(buyer, query, "cream", "web", state, rules, after_sales, as_of=as_of)
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
                         "order_id": actual.get("order_id") or actual.get("order", {}).get("id"), "first_kind": replies[0]["kind"],
                         "checks": actual.get("checks"), "excerpt": citation.get("excerpt"),
                         "status": (actual.get("after_sale") or {}).get("status")}
        for key, value in expected.items():
            if key == "text" and expected["kind"] in ("policy", "gift_review"):
                ok = actual["text"] == value
            elif key == "text":
                ok = value in actual["text"]
            else:
                ok = actual_fields[key] == value
            if not ok:
                findings.append(key)
        if expected["kind"] == "policy" and citation.get("excerpt") != expected["text"]:
            findings.append("excerpt")
        if expected["kind"] == "unavailable" and ("order" in actual or "citation" in actual):
            findings.append("private_data")
        if "rule_id" in expected:
            rankings.append({"query": identifier, "relevant_ids": [expected["rule_id"]],
                             "ranked_ids": [citation["rule_id"]] if citation.get("rule_id") else []})
        results.append({"id": identifier, "category": category, "queries": queries, "expected": expected,
                        "replies": replies, "passed": not findings, "differences": findings})
    return results, rankings
