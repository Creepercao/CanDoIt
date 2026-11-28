from datetime import date

import pytest

from backend import customer_quality
from backend.customer_service import RULES


def test_draft_boundary_dates_group_adjacent_checkpoints_and_handle_open_end():
    assert customer_quality.draft_boundary_dates("2026-12-01", "2026-11-24", "2026-11-24", "2026-11-25") == [
        (date(2026, 11, 23), ["before_start"]),
        (date(2026, 11, 24), ["starts", "base_cutoff", "last_active"]),
        (date(2026, 11, 25), ["expires"]),
        (date(2026, 12, 1), ["base_expires"]),
    ]
    assert customer_quality.draft_boundary_dates(None, "2026-12-01", "2026-12-01", None) == [
        (date(2026, 11, 30), ["before_start"]),
        (date(2026, 12, 1), ["starts", "base_cutoff"]),
    ]


@pytest.mark.parametrize(("case_id", "query_prefix", "change", "difference"), [
    ("historical", "DEMO-1001 下单时", "amount", "text"),
    ("current-web", "当前活动", "promise", "text"),
    ("current-live", "直播间赠品", "excerpt", "excerpt"),
])
def test_policy_case_detects_wrong_body_or_evidence_with_correct_rule_id(
    monkeypatch, case_id, query_prefix, change, difference,
):
    original = customer_quality.conversation_turn

    def corrupt_reply(buyer, message, *args, **kwargs):
        reply, state = original(buyer, message, *args, **kwargs)
        if message.startswith(query_prefix):
            if change == "amount":
                reply["text"] = reply["text"].replace("299 元", "1299 元")
            elif change == "promise":
                reply["text"] += "所有顾客都可以额外获赠一份。"
            else:
                reply["citation"]["excerpt"] = "其他活动的规则。"
        return reply, state

    monkeypatch.setattr(customer_quality, "conversation_turn", corrupt_reply)
    report = customer_quality.run_quality(RULES)
    case = next(item for item in report["cases"] if item["id"] == case_id)
    assert not case["passed"]
    assert case["differences"] == [difference]
    assert report["passed"] == report["total"] - 1
    assert report["policy_recall_at_1"] == 1


def test_citation_denominator_excludes_intentional_campaign_gap():
    active = customer_quality.run_quality(RULES)
    paused = tuple((*rule[:6], "2026-11-22", rule[7]) if rule[0] == "gift-nov-current" else rule for rule in RULES)
    report = customer_quality.run_quality(paused)
    case = next(item for item in report["cases"] if item["id"] == "current-web")
    assert case["passed"] and case["expected"]["kind"] == "handoff"
    assert report["policy_citation_queries"] == active["policy_citation_queries"] - 2
    assert report["catalog_sha256"] != active["catalog_sha256"]


def test_quality_uses_simulated_date_for_expectations_and_both_paths():
    report = customer_quality.run_quality(RULES, offline=True, as_of=date(2026, 12, 1))
    cases = {case["id"]: case for case in report["cases"]}
    assert report["evaluation_date"] == "2026-12-01"
    current = cases["current-web"]
    assert current["expected"]["kind"] == current["replies"][-1]["kind"] == current["baseline_replies"][-1]["kind"] == "handoff"
    assert cases["historical"]["replies"][-1]["citation"]["effective_date"] == "2026-11-02"
