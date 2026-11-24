"""Paired end-to-end routing benchmark on fixed synthetic paraphrases."""

import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backend.customer_intent import classify
from backend.customer_service import RULES, support_turn


# Each case is a new buyer-A session on the brand store, with cream selected.
# Each turn has its own expected reply fields. No transcript is fed into the classifier.
CASES = [
    [("DEMO-1001 开箱只有正装，搭的那一份去哪儿了", {"kind": "gift_review", "rule": "gift-nov-old"})],
    [("箱子里少了说好随单给的小瓶", {"kind": "clarify", "contains": "订单号"}),
     ("DEMO-1002", {"kind": "gift_review", "rule": "gift-nov-current"})],
    [("DEMO-1002 旅行装是不是漏装了呀", {"kind": "gift_review", "rule": "gift-nov-current"})],
    [("品牌商城现在凑到多少钱能白拿一份？", {"kind": "policy", "rule": "gift-nov-current"})],
    [("主播那边说有福利，直播间买到什么价位才送？", {"kind": "policy", "rule": "gift-live"})],
    [("现在满赠是按单笔算的吗", {"kind": "policy", "rule": "gift-nov-current"})],
    [("DEMO-1001 我付款那天附送小瓶的门槛是多少", {"kind": "policy", "rule": "gift-nov-old"})],
    [("我买的时候满多少才有赠品来着？", {"kind": "clarify", "contains": "订单号"}),
     ("DEMO-1001", {"kind": "policy", "rule": "gift-nov-old"})],
    [("DEMO-1002 有快递员取走了吗", {"kind": "order", "order": "DEMO-1002", "contains": "待发货"})],
    [("我那件东西出仓了吗", {"kind": "clarify", "contains": "订单号"}),
     ("DEMO-1001", {"kind": "order", "order": "DEMO-1001", "contains": "已发货"})],
    [("这罐面霜有多重啊", {"kind": "policy", "rule": "cream-info"})],
    [("氨基酸洁面一管装多少？", {"kind": "policy", "rule": "cleanser-info"})],
    [("这个的配料上哪儿看呀", {"kind": "policy", "rule": "cream-info"})],
    [("还没开封，不想留了，要走什么流程", {"kind": "policy", "rule": "returns-web"})],
    [("直播间拍的不合适要怎么寄回去", {"kind": "policy", "rule": "returns-live"})],
    [("退换要店里先核实吗", {"kind": "policy", "rule": "returns-web"})],
    [("DEMO-1001 昨天交的售后单子现在谁在处理", {"kind": "after_sale", "contains": "人工审核中"})],
    [("DEMO-1002 帮看下售后进度呗", {"kind": "after_sale", "contains": "暂无售后申请"})],
    [("让个真人接一下，这事我想直接聊", {"kind": "handoff"})],
    [("面霜用了刺痛，麻烦帮忙处理", {"kind": "handoff"})],
    [("DEMO-2001 这个包裹有人取走了没", {"kind": "unavailable"})],
    [("DEMO-1001 和 DEMO-1002 都查下现在到哪一步了", {"kind": "clarify", "contains": "一个订单号"})],
    [("我那件出了仓没？顺带问下不合适咋寄回去？", {"kind": "clarify", "multiple": True})],
    [("你们周日线下几点关门呀", {"kind": "clarify"})],
]


def run_arm(model):
    results = []
    with patch.dict(os.environ, {"SUPPORT_INTENT_MODEL": model}):
        for case in CASES:
            state, turns = {}, []
            for query, expected in case:
                label, source = classify(query)
                reply, state = support_turn("a", query, "cream", "web", state, RULES,
                                            {"DEMO-1001": {"status": "reviewing"}}, (label, source))
                citation = reply.get("citation", {})
                actual = {"kind": reply["kind"], "rule": citation.get("rule_id"),
                          "order": reply.get("order", {}).get("id"),
                          "contains": expected.get("contains", "") in reply["text"],
                          "multiple": any(s in reply["text"] for s in ("几件事", "分开咨询", "先选择一项"))}
                passed = all(actual[k] == (True if k == "contains" else v) for k, v in expected.items())
                if expected["kind"] == "gift_review":
                    passed &= reply.get("checks") == ["订单实付金额", "赠品出库记录"]
                turns.append(dict(query=query, expected=expected, label=label, source=source,
                                  kind=reply["kind"], rule=citation.get("rule_id"), text=reply["text"], passed=passed))
            results.append({"passed": all(t["passed"] for t in turns), "turns": turns})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.model.strip():
        parser.error("supply a model ID for the enabled arm")
    offline, enabled = run_arm(""), run_arm(args.model)
    rows = [dict(id=i, offline=a, enabled=b) for i, (a, b) in enumerate(zip(offline, enabled), 1)]
    sources = Counter(t["source"] for case in enabled for t in case["turns"])
    summary = {"cases": len(rows), "offline_pass": sum(a["passed"] for a in offline),
               "enabled_pass": sum(b["passed"] for b in enabled), "enabled_sources": dict(sources),
               "gains": [r["id"] for r in rows if not r["offline"]["passed"] and r["enabled"]["passed"]],
               "losses": [r["id"] for r in rows if r["offline"]["passed"] and not r["enabled"]["passed"]]}
    report = dict(model=args.model, timestamp=datetime.now(timezone.utc).isoformat(),
                  case_sha256=hashlib.sha256(json.dumps(CASES, ensure_ascii=False).encode()).hexdigest(),
                  summary=summary, rows=rows)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))
    if not sources["model"]:
        raise SystemExit("No model labels used. The enabled arm does not demonstrate live inference.")


if __name__ == "__main__":
    main()
