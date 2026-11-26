from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.api.customer_service import get_store
from backend.customer_service import SupportStore
from backend.main import app


@pytest.fixture
def api(tmp_path, monkeypatch):
    store = tmp_path / "support.sqlite"
    monkeypatch.setitem(app.dependency_overrides, get_store, lambda: SupportStore(store))
    return TestClient(app)


PREFIX = "/api/customer-service"


def session(api, buyer="a"):
    response = api.post(PREFIX + "/sessions", json={"buyer": buyer})
    assert response.status_code == 201
    data = response.json()
    return {"X-Demo-Session": data["token"]}, data


def ask(api, headers, text, **options):
    response = api.post(PREFIX + "/messages", headers=headers, json={"message": text, **options})
    assert response.status_code == 200
    return response.json()


def staff_session(api):
    response = api.post(PREFIX + "/staff/sessions")
    assert response.status_code == 201
    return {"X-Demo-Staff": response.json()["token"]}


def test_confirmed_gift_review_keeps_original_evidence(api, monkeypatch):
    monkeypatch.delenv("SUPPORT_INTENT_MODEL", raising=False)
    customer, _ = session(api)
    other, _ = session(api)
    assert ask(api, customer, "小样是不是漏发了？")["kind"] == "clarify"
    assert ask(api, customer, "DEMO-2001")["kind"] == "unavailable"
    answer = ask(api, customer, "DEMO-1001")
    assert answer["kind"] == "gift_review" and answer["issue"] == "小样是不是漏发了？"
    assert answer["citation"]["rule_id"] == "gift-nov-old"
    ask(api, customer, "现在的赠品规则？")
    assert api.get(PREFIX + "/session", headers=customer).json()["tickets"] == []
    body = {"request_id": str(uuid4()), "summary": answer["issue"], "order_id": "DEMO-1001",
            "message_id": answer["message_id"]}
    path = PREFIX + "/tickets"
    assert api.post(path, headers=other, json=body).status_code == 409
    assert api.post(path, headers=customer, json={**body, "order_id": "DEMO-1002"}).status_code == 409
    ticket = api.post(path, headers=customer, json=body).json()
    assert api.post(path, headers=customer, json=body).json() == ticket
    duplicate = {**body, "request_id": str(uuid4()), "summary": "再次点击原消息"}
    assert api.post(path, headers=customer, json=duplicate).json() == ticket
    assert api.get(PREFIX + "/session", headers=customer).json()["tickets"] == [ticket]
    assert api.post(path, headers=customer, json={**body, "message_id": None}).status_code == 409
    detail = api.get(PREFIX + "/staff/tickets/" + ticket["id"], headers=staff_session(api)).json()
    assert detail["citation"] == answer["citation"]
    assert detail["checks"] == ["订单实付金额", "赠品出库记录"]


@pytest.mark.parametrize("mode", ["valid", "timeout", "invalid"])
def test_model_classification_transport_and_fallback(api, monkeypatch, mode):
    import httpx
    import json
    from backend import config
    from backend.models import provider

    monkeypatch.setenv("SUPPORT_INTENT_MODEL", "test/chat")
    monkeypatch.setattr(config, "PROVIDERS", [config.ProviderConfig(name="test", base_url="http://test/v1", api_key="demo")])
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        if mode == "timeout":
            raise httpx.ReadTimeout("test timeout", request=request)
        label = "missing_gift" if mode == "valid" else "已批准退款"
        return httpx.Response(200, json={"id": "test", "model": "test/chat", "created": 1,
            "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": label}}]})

    factory = provider.create_chat_model
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(provider, "create_chat_model", lambda *a, **kw: factory(*a, **kw, http_client=client))
        customer, _ = session(api)
        query = "结账时说随箱附的小东西怎么开箱只见正装" if mode == "valid" else "旅行装没收到"
        answer = ask(api, customer, "ｄｅｍｏ－１００１ " + query + "，电话 +86 13800138000，邮箱 buyer@example.test。")
    assert len(calls) == 1
    assert calls[0]["model"] == "test/chat"
    assert calls[0]["messages"][-1]["content"] == "[订单号] " + query + ",电话 [手机号],邮箱 [邮箱]。"
    assert answer["intent_source"] == ("model" if mode == "valid" else "fallback")
    assert answer["kind"] == "gift_review" and answer["citation"]["rule_id"] == "gift-nov-old"


def test_policy_uses_effective_versions_and_own_order_scope(api):
    headers, _ = session(api)
    current = ask(api, headers, "现在满299元有赠品吗？")
    assert "399" in current["text"] and "299" not in current["text"]
    assert current["citation"]["rule_id"] == "gift-nov-current"
    historical = ask(api, headers, "DEMO-1001 下单时有什么赠品规则？", channel="live")
    assert "299" in historical["text"]
    assert historical["citation"]["effective_date"] == "2026-11-02"
    assert historical["citation"]["snapshot_id"] != current["citation"]["snapshot_id"]
    assert historical["text"] in historical["citation"]["excerpt"]
    assert "499" in ask(api, headers, "直播间的赠品规则？")["text"]
    assert "100 毫升" in ask(api, headers, "氨基酸洁面的规格？")["text"]


def test_order_lookup_does_not_reveal_another_buyer_order(api):
    a, data = session(api)
    b, _ = session(api, "b")
    assert {order["id"] for order in data["orders"]} == {"DEMO-1001", "DEMO-1002"}
    assert ask(api, b, "DEMO-2001 的物流")["order"]["status"] == "已签收"
    denied = ask(api, a, "查 DEMO-2001 的物流")
    unknown = ask(api, a, "查 DEMO-9999 的物流")
    assert denied["kind"] == unknown["kind"] == "unavailable"
    assert denied["text"] == unknown["text"] and "order" not in denied
    assert ask(api, a, "DEMO-2001 下单时的赠品")["kind"] == "unavailable"
    assert ask(api, a, "物流到哪了？")["kind"] == "clarify"
    assert api.get(PREFIX + "/session").status_code == 401


def test_order_policy_verifies_ownership_and_rejects_conflicting_scope(api):
    a, _ = session(api)
    b, _ = session(api, "b")
    assert ask(api, a, "DEMO-2001 退换规则是什么？")["kind"] == "unavailable"
    assert ask(api, b, "DEMO-2001 退换规则是什么？")["citation"]["rule_id"] == "returns-live"
    conflicting = ask(api, b, "DEMO-2001 的品牌商城退换规则是什么？")
    assert conflicting["kind"] == "clarify" and "citation" not in conflicting
    assert ask(api, b, "品牌商城退换规则是什么？")["citation"]["rule_id"] == "returns-web"


def test_colloquial_mixed_questions_require_selection_even_when_model_picks_one(api, monkeypatch):
    monkeypatch.setattr("backend.customer_intent.classify", lambda _: ("progress", "model"))
    headers, _ = session(api)
    mixed = ask(api, headers, "我那件出了仓没？顺带问下不合适咋寄回去？")
    assert mixed["kind"] == "clarify" and "先帮你查哪一件" in mixed["text"]
    single = ask(api, headers, "DEMO-1001 退货物流进度")
    assert "几件事" not in single["text"]


def test_model_cannot_replace_explicit_order_operations_or_their_followup(api, monkeypatch):
    monkeypatch.setattr("backend.customer_intent.classify", lambda _: ("missing_gift", "model"))
    headers, _ = session(api)
    clarification = ask(api, headers, "查物流")
    assert clarification["kind"] == "clarify" and clarification["intent_source"] == "rules"
    monkeypatch.setattr("backend.customer_intent.classify", lambda query: (
        ("unknown", "rules") if query == "DEMO-1001" else ("missing_gift", "model")
    ))
    assert ask(api, headers, "DEMO-1001")["order"]["id"] == "DEMO-1001"
    shipping = ask(api, headers, "DEMO-1001 物流")
    progress = ask(api, headers, "DEMO-1001 售后进度")
    assert (shipping["kind"], progress["kind"]) == ("order", "after_sale")
    assert shipping["intent_source"] == progress["intent_source"] == "rules"


def test_handoff_takes_priority_over_status_query(api):
    headers, _ = session(api)
    mixed = ask(api, headers, "DEMO-1001 用过以后感觉不适，顺便查一下售后进度。")
    assert mixed["kind"] == "handoff" and mixed["order_id"] == "DEMO-1001"
    status = ask(api, headers, "DEMO-1001 售后进度是人工审核中吗？")
    assert status["kind"] == "after_sale"


def test_handoff_has_explicit_creation_order_ownership_and_lifecycle(api):
    headers, _ = session(api)
    other, _ = session(api)
    result = ask(api, headers, "DEMO-1001 使用面霜后觉得不适，怎么退款？")
    assert result["kind"] == "handoff" and result["order_id"] == "DEMO-1001"
    assert api.get(PREFIX + "/session", headers=headers).json()["tickets"] == []
    body = {"request_id": uuid4().hex, "order_id": result["order_id"], "summary": "虚构售后反馈"}
    assert api.post(PREFIX + "/tickets", headers=headers, json={**body, "order_id": "DEMO-2001"}).status_code == 404
    created = api.post(PREFIX + "/tickets", headers=headers, json=body)
    assert created.status_code == 201
    ticket = created.json()
    assert api.post(PREFIX + "/tickets", headers=headers, json=body).json() == ticket
    assert api.post(PREFIX + "/tickets", headers=headers, json={**body, "summary": "不同内容"}).status_code == 409
    path = PREFIX + "/staff/tickets/" + ticket["id"]
    staff = staff_session(api)
    other_staff = staff_session(api)
    assert api.get(PREFIX + "/session", headers=other).json()["tickets"] == []
    assert api.get(path, headers=other).status_code == 401
    assert api.get(path, headers=staff).json()["order"]["id"] == "DEMO-1001"
    assert api.post(path + "/actions", headers=staff, json={"action": "resolve", "text": "已接手"}).status_code == 409
    assert api.post(path + "/actions", headers=staff, json={"action": "claim"}).json()["status"] == "in_progress"
    assert api.get(path, headers=other_staff).status_code == 404
    assert ask(api, headers, "还有一个问题")["kind"] == "human_pending"
    assert api.post(path + "/actions", headers=staff, json={"action": "reply", "text": "已查看记录"}).status_code == 200
    closed = api.post(path + "/actions", headers=staff, json={"action": "resolve", "text": "已记录并回复顾客"}).json()
    assert closed["status"] == "resolved"
    customer = api.get(PREFIX + "/session", headers=headers).json()
    assert customer["tickets"][0]["status"] == "resolved"
    assert [item["answer"]["text"] for item in customer["messages"] if item["answer"]["kind"] == "human"] == ["已查看记录", "已记录并回复顾客"]
    assert ask(api, headers, "送赠品吗？")["kind"] == "policy"


def test_multi_turn_carries_pending_intent_and_latest_verified_order(api):
    b, _ = session(api, "b")
    assert ask(api, b, "多久发货？")["kind"] == "clarify"
    assert ask(api, b, "DEMO-1001")["kind"] == "unavailable"
    assert ask(api, b, "ＤＥＭＯ－２００１。")["order"]["status"] == "已签收"
    follow_up = ask(api, b, "那能退吗？")
    assert follow_up["citation"]["rule_id"] == "returns-live"
    a, _ = session(api, "a")
    assert ask(api, a, "刚买的时候送什么赠品？")["kind"] == "clarify"
    assert ask(api, a, "DEMO-1001")["citation"]["rule_id"] == "gift-nov-old"
    assert len(api.get(PREFIX + "/session", headers=a).json()["messages"]) == 2


def test_aftersales_is_idempotent_and_tracks_claim_and_resolution(api):
    customer, _ = session(api)
    operator = staff_session(api)
    path = PREFIX + "/after-sales"
    body = {"order_id": "DEMO-1001", "summary": "模拟退换"}
    assert api.post(path, headers=customer, json={**body, "order_id": "DEMO-2001"}).status_code == 404
    sale = api.post(path, headers=customer, json=body).json()
    assert sale["status"] == "submitted"
    assert api.post(path, headers=customer, json=body).json()["id"] == sale["id"]
    ticket = PREFIX + f"/staff/tickets/{sale['ticket_id']}/actions"
    assert api.post(ticket, headers=operator, json={"action": "claim"}).status_code == 200
    current = ask(api, customer, "DEMO-1001 售后进度")
    assert current["after_sale"]["status"] == "reviewing"
    assert api.post(ticket, headers=operator, json={"action": "resolve", "text": "已处理模拟申请"}).status_code == 200
    assert ask(api, customer, "那申请进度呢？")["after_sale"]["status"] == "completed"
    assert len(api.get(PREFIX + "/session", headers=customer).json()["after_sales"]) == 1


def test_releasing_a_ticket_preserves_transcript_and_allows_another_operator(api):
    customer, _ = session(api)
    first, second = staff_session(api), staff_session(api)
    sale = api.post(PREFIX + "/after-sales", headers=customer,
                    json={"order_id": "DEMO-1001", "summary": "等待另一位客服核对"}).json()
    path = PREFIX + f"/staff/tickets/{sale['ticket_id']}/actions"
    assert api.post(path, headers=first, json={"action": "claim"}).status_code == 200
    assert api.post(path, headers=first, json={"action": "reply", "text": "正在核对订单"}).status_code == 200
    assert api.post(path, headers=second, json={"action": "release"}).status_code == 404
    assert api.post(path, headers=first, json={"action": "release"}).json()["released"] is True
    view = api.get(PREFIX + "/session", headers=customer).json()
    assert view["after_sales"][0]["status"] == "submitted"
    assert view["messages"][-1]["answer"]["kind"] == "system"
    assert api.post(path, headers=first, json={"action": "reply", "text": "工单已退回"}).status_code == 409
    handover = api.post(path, headers=second, json={"action": "claim"}).json()
    assert handover["messages"][0]["answer"]["text"] == "正在核对订单"
    assert api.post(path, headers=second, json={"action": "resolve", "text": "已核对订单"}).status_code == 200
    assert ask(api, customer, "DEMO-1001 售后进度")["after_sale"]["status"] == "completed"


def test_rule_draft_preview_publish_and_historical_preservation(api):
    customer, _ = session(api)
    operator = staff_session(api)
    before = api.post(PREFIX + "/staff/quality", headers=operator).json()
    root = PREFIX + "/staff/rules"
    model = {"base_rule_id": "gift-nov-current", "starts_at": "2026-11-23", "ends_at": "2026-12-01", "body": "实付满 459 元赠旅行装。"}
    assert api.post(root + "/drafts", headers=customer, json=model).status_code == 401
    assert api.post(root + "/drafts", headers=operator, json={**model, "starts_at": "2026-11-22"}).status_code == 409
    preview = api.post(root + "/drafts", headers=operator, json=model).json()
    assert "399" in preview["previous"]["body"]
    assert preview["impact"] == {"publishable": True, "old_ends_at": "2026-11-23", "new_starts_at": "2026-11-23", "gap": None}
    scheduled = api.post(root + "/drafts", headers=operator, json={
        **model, "starts_at": "2026-12-03", "ends_at": None,
    }).json()
    assert scheduled["impact"]["gap"] == {"starts_at": "2026-12-01", "ends_at": "2026-12-03"}
    assert "399" in ask(api, customer, "现在满多少送赠品？")["text"]
    path = root + "/drafts/" + preview["draft"]["id"]
    amended = {**model, "body": "实付满 469 元赠旅行装。\n# 赠品说明\n以订单页为准。"}
    assert api.patch(path, headers=operator, json=amended).status_code == 422
    new = api.patch(path, headers=operator, json={**amended, "draft_sha256": preview["draft_sha256"]}).json()
    assert api.patch(path, headers=staff_session(api), json={**model, "draft_sha256": preview["draft_sha256"]}).status_code == 409
    assert api.get(path, headers=operator).json()["draft"]["body"] == amended["body"]
    assert api.post(path + "/publish", headers=operator, json={"draft_sha256": preview["draft_sha256"]}).status_code == 409
    published = api.post(path + "/publish", headers=operator, json={"draft_sha256": new["draft_sha256"]}).json()
    stale = api.get(f"{root}/drafts/{scheduled['draft']['id']}", headers=operator).json()
    assert not stale["impact"]["publishable"]
    assert api.patch(path, headers=operator, json={**model, "draft_sha256": new["draft_sha256"]}).status_code == 409
    current = ask(api, customer, "现在满多少送赠品？")
    assert current["text"] == amended["body"]
    assert current["citation"]["rule_id"] == published["published"]["id"]
    assert "299" in ask(api, customer, "DEMO-1001 下单时有什么赠品规则？")["text"]
    assert "399" in ask(api, customer, "DEMO-1002 下单时有什么赠品规则？")["text"]
    report = api.post(PREFIX + "/staff/quality", headers=operator).json()
    assert report["catalog_sha256"] != before["catalog_sha256"]
    assert report["case_sha256"] == before["case_sha256"] and report["comparison_run_id"] is None
    assert report["passed"] == report["total"] and report["policy_recall_at_1"] == 1


def test_quality_retains_failure_evidence_and_reports_regression(api, monkeypatch):
    import backend.customer_quality as quality
    operator = staff_session(api)
    path = PREFIX + "/staff/quality"
    good = api.post(path, headers=operator).json()
    original = quality.conversation_turn
    def regress(buyer, message, *args):
        if message.startswith("DEMO-1001 下单时"):
            return {"kind": "clarify", "text": "演示规则缺失"}, {}
        return original(buyer, message, *args)
    monkeypatch.setattr(quality, "conversation_turn", regress)
    bad = api.post(path, headers=operator).json()
    assert bad["passed"] == good["passed"] - 1
    assert bad["regressed"] == ["historical"]
    assert bad["comparison_run_id"] == good["id"]
    assert "rule_id" in next(case["differences"] for case in bad["cases"] if case["id"] == "historical")
    assert len(api.get(path, headers=operator).json()) == 2


def test_draft_quality_previews_live_text_without_publishing_or_calling_model(api, monkeypatch):
    from backend import customer_quality

    monkeypatch.setenv("SUPPORT_INTENT_MODEL", "configured-but-not-called")
    original_classify = customer_quality.classify

    def offline_only(query, *, model_name=None):
        assert model_name == ""
        return original_classify(query, model_name=model_name)

    monkeypatch.setattr(customer_quality, "classify", offline_only)
    operator = staff_session(api)
    root = PREFIX + "/staff/rules"
    draft = api.post(root + "/drafts", headers=operator, json={
        "base_rule_id": "gift-nov-current", "starts_at": "2026-11-23", "body": "实付满 459 元赠旅行装。",
    }).json()
    path = f"{root}/drafts/{draft['draft']['id']}"
    evaluated = api.get(path + "/quality", headers=operator).json()
    assert evaluated["draft_sha256"] == draft["draft_sha256"]
    by_id = {item["id"]: item for item in evaluated["changes"]}
    assert "399" in by_id["current-web"]["current_replies"][-1]["text"]
    assert "459" in by_id["current-web"]["proposed_replies"][-1]["text"]
    assert "historical" not in by_id
    assert api.get(root, headers=operator).json()["drafts"][0]["published_id"] is None
    assert api.get(PREFIX + "/staff/quality", headers=operator).json() == []
    published = api.post(path + "/publish", headers=operator, json={"draft_sha256": draft["draft_sha256"]}).json()
    assert published["published"]["id"] == by_id["current-web"]["proposed_replies"][-1]["citation"]["rule_id"]


def test_quality_report_download_preserves_the_selected_run(api, monkeypatch):
    import backend.customer_quality as quality
    operator = staff_session(api)
    path = PREFIX + "/staff/quality"
    first = api.post(path, headers=operator).json()
    original = quality.conversation_turn

    def bad_historical_amount(buyer, message, *args):
        answer, state = original(buyer, message, *args)
        if message.startswith("DEMO-1001 下单时"):
            answer["text"] = "实付满 1299 元 | 错误示例"
        return answer, state

    monkeypatch.setattr(quality, "conversation_turn", bad_historical_amount)
    second = api.post(path, headers=operator).json()
    assert first["id"] != second["id"]
    assert first == api.get(path, headers=operator).json()[1]
    first_path = f"{path}/{first['id']}/report"
    assert api.get(first_path).status_code == 401
    original_report = api.get(first_path, headers=operator)
    current = api.get(f"{path}/{second['id']}/report", headers=operator)
    assert original_report.status_code == current.status_code == 200
    assert "text/markdown" in current.headers["Content-Type"]
    assert current.headers["Cache-Control"] == "no-store"
    assert f"customer-quality-{second['id']}.md" in current.headers["Content-Disposition"]
    assert "historical · 政策版本 · 通过" in original_report.text
    assert "historical · 政策版本 · 失败" in current.text
    assert second["catalog_sha256"] in current.text
    assert "实付满 1299 元 &#124; 错误示例" in current.text
    assert api.get(f"{path}/98765/report", headers=operator).status_code == 404
