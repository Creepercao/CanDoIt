"""API for CanDoIt's local e-commerce demo."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path as ApiPath, Response
from pydantic import BaseModel, Field, StringConstraints

from backend.customer_quality import BOUNDARY_MARKERS, compare_draft_quality, draft_boundary_dates, run_quality
from backend.customer_quality_report import render_quality_report
from backend.customer_service import CHANNELS, DEMO_DATE, ORDERS, PRODUCTS, SupportStore, visible_order

router = APIRouter(prefix="/customer-service", tags=["customer-service"])
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
OrderId = Annotated[str, StringConstraints(pattern=r"^DEMO-\d{4}$")]
DraftHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class StartSession(BaseModel):
    buyer: Literal["a", "b"] = "a"


class Ask(BaseModel):
    message: Text
    product: Literal["cream", "cleanser"] = "cream"
    channel: Literal["web", "live"] = "web"


class SubmitTicket(BaseModel):
    request_id: UUID
    order_id: OrderId | None = None
    summary: Text
    message_id: Annotated[int, Field(ge=1)] | None = None


class AfterSale(BaseModel):
    order_id: OrderId
    summary: Text


class StaffAction(BaseModel):
    action: Literal["claim", "reply", "resolve", "release"]
    text: Text | None = None


class RuleDraft(BaseModel):
    base_rule_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    starts_at: date
    ends_at: date | None = None
    body: Text


class PublishRule(BaseModel):
    draft_sha256: DraftHash


class EditRuleDraft(RuleDraft):
    draft_sha256: DraftHash


def get_store():
    return SupportStore(Path(__file__).resolve().parents[2] / "temp" / "customer-service.sqlite")


@dataclass(frozen=True)
class DemoSession:
    buyer: str
    token: str
    store: SupportStore


def get_session(
    response: Response,
    store: Annotated[SupportStore, Depends(get_store)],
    token: Annotated[str | None, Header(alias="X-Demo-Session", max_length=64)] = None,
):
    buyer = store.buyer_for(token) if token else None
    if buyer is None:
        raise HTTPException(401, "请重新开启体验会话。")
    response.headers["Cache-Control"] = "no-store"
    return DemoSession(buyer, token, store)


def describe_session(session):
    return {"buyer": session.buyer, "demo_date": DEMO_DATE.isoformat(), "products": PRODUCTS, "channels": CHANNELS,
            "orders": [visible_order(session.buyer, key) for key, order in ORDERS.items() if order["buyer"] == session.buyer],
            **session.store.conversation(session.token)}


@router.post("/sessions", status_code=201)
def start_session(request: StartSession, response: Response, store: Annotated[SupportStore, Depends(get_store)]):
    response.headers["Cache-Control"] = "no-store"
    token = store.create_session(request.buyer)
    session = DemoSession(request.buyer, token, store)
    return {"token": token, **describe_session(session)}


@router.get("/session")
def read_session(session: Annotated[DemoSession, Depends(get_session)]):
    return describe_session(session)


@router.post("/messages")
def send_message(request: Ask, session: Annotated[DemoSession, Depends(get_session)]):
    return session.store.ask(session.token, session.buyer, request.message, request.product, request.channel)


@router.post("/tickets", status_code=201)
def submit_ticket(request: SubmitTicket, session: Annotated[DemoSession, Depends(get_session)]):
    if request.order_id and visible_order(session.buyer, request.order_id) is None:
        raise HTTPException(404, "当前体验身份下查不到该订单。")
    try:
        return session.store.submit_ticket(session.token, str(request.request_id), request.order_id, request.summary,
                                          request.message_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/after-sales", status_code=201)
def apply_after_sale(request: AfterSale, session: Annotated[DemoSession, Depends(get_session)]):
    if visible_order(session.buyer, request.order_id) is None:
        raise HTTPException(404, "当前体验身份下查不到该订单。")
    return session.store.apply_after_sale(session.token, request.order_id, request.summary)


@dataclass(frozen=True)
class StaffSession:
    id: str
    store: SupportStore


@router.post("/staff/sessions", status_code=201)
def start_staff(response: Response, store: Annotated[SupportStore, Depends(get_store)]):
    response.headers["Cache-Control"] = "no-store"
    return {"token": store.start_staff()}


def get_staff(
    response: Response,
    store: Annotated[SupportStore, Depends(get_store)],
    token: Annotated[str | None, Header(alias="X-Demo-Staff", max_length=64)] = None,
):
    staff_id = store.staff_id(token) if token else None
    if staff_id is None:
        raise HTTPException(401, "请重新进入模拟客服工作台。")
    response.headers["Cache-Control"] = "no-store"
    return StaffSession(staff_id, store)


Staff = Annotated[StaffSession, Depends(get_staff)]


@router.get("/staff/tickets")
def staff_queue(staff: Staff):
    return staff.store.queue(staff.id)


@router.get("/staff/tickets/{ticket_id}")
def staff_ticket(ticket_id: UUID, staff: Staff):
    try:
        return staff.store.ticket_detail(ticket_id.hex, staff.id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/staff/tickets/{ticket_id}/actions")
def handle_ticket(ticket_id: UUID, request: StaffAction, staff: Staff):
    needs_text = request.action in ("reply", "resolve")
    if needs_text and not request.text:
        raise HTTPException(422, "回复和结单需要填写内容。")
    if not needs_text and request.text is not None:
        raise HTTPException(422, "受理或退回工单无需填写处理说明。")
    try:
        staff.store.act_on_ticket(ticket_id.hex, staff.id, request.action, request.text)
        if request.action == "release":
            return {"id": ticket_id.hex, "released": True}
        return staff.store.ticket_detail(ticket_id.hex, staff.id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/staff/rules")
def rule_catalog(staff: Staff):
    return staff.store.rule_catalog()


@router.post("/staff/rules/drafts", status_code=201)
def save_draft(request: RuleDraft, staff: Staff):
    return write_draft(request, staff.store)


@router.patch("/staff/rules/drafts/{draft_id}")
def edit_draft(draft_id: UUID, request: EditRuleDraft, staff: Staff):
    return write_draft(request, staff.store, draft_id.hex, request.draft_sha256)


def write_draft(request, store, draft_id=None, reviewed_hash=None):
    try:
        draft_id = store.save_rule_draft(request.base_rule_id, request.starts_at.isoformat(),
                                         request.ends_at.isoformat() if request.ends_at else None, request.body,
                                         draft_id, reviewed_hash=reviewed_hash)
        return store.preview_rule(draft_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/staff/rules/drafts/{draft_id}")
def preview_draft(draft_id: UUID, staff: Staff):
    try:
        return staff.store.preview_rule(draft_id.hex)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/staff/rules/drafts/{draft_id}/publish")
def publish_draft(draft_id: UUID, request: PublishRule, staff: Staff):
    try:
        staff.store.publish_rule(draft_id.hex, request.draft_sha256)
        return staff.store.preview_rule(draft_id.hex)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def draft_quality_snapshot(draft_id, staff):
    try:
        return staff.store.draft_quality_catalogs(draft_id.hex)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/staff/rules/drafts/{draft_id}/quality")
def preview_draft_quality(draft_id: UUID, staff: Staff, as_of: date = DEMO_DATE):
    current, proposed, draft_hash, _ = draft_quality_snapshot(draft_id, staff)
    return {**compare_draft_quality(current, proposed, as_of=as_of), "draft_sha256": draft_hash}


@router.get("/staff/rules/drafts/{draft_id}/quality/boundaries")
def preview_draft_boundaries(draft_id: UUID, staff: Staff):
    current, proposed, draft_hash, window = draft_quality_snapshot(draft_id, staff)
    checks = [{"markers": markers, **compare_draft_quality(current, proposed, as_of=day)}
              for day, markers in draft_boundary_dates(**window)]
    return {"draft_sha256": draft_hash, "marker_labels": BOUNDARY_MARKERS, "checks": checks}


@router.get("/staff/quality")
def quality_history(staff: Staff):
    return staff.store.quality_history()


@router.post("/staff/quality", status_code=201)
def run_regression(staff: Staff):
    return staff.store.record_quality(run_quality(staff.store.rules()))


@router.get("/staff/quality/{run_id}/report")
def export_regression_report(run_id: Annotated[int, ApiPath(ge=1, le=2**63 - 1)], staff: Staff):
    report = staff.store.get_quality_run(run_id)
    if report is None:
        raise HTTPException(404, "找不到该次评估。")
    return Response(render_quality_report(report), media_type="text/markdown", headers={
        "Cache-Control": "no-store",
        "Content-Disposition": f'attachment; filename="customer-quality-{run_id}.md"',
    })
