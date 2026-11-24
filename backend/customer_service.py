"""Local e-commerce support workflow using synthetic shop data."""

from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
from textwrap import dedent, indent
import unicodedata
from uuid import uuid4

from backend.chunking import prepare_document
from backend.hierarchy import HierarchyView, _payload, _snapshot_id


DEMO_DATE = date(2026, 11, 23)
PRODUCTS = {"cream": "舒缓面霜", "cleanser": "氨基酸洁面"}
CHANNELS = {"web": "品牌商城", "live": "直播间"}
ORDERS = {
    "DEMO-1001": {"buyer": "a", "product": "cream", "channel": "web", "purchased": "2026-11-02",
                  "status": "已发货", "shipping": "模拟物流已揽收，尚无预计送达时间。"},
    "DEMO-1002": {"buyer": "a", "product": "cleanser", "channel": "web", "purchased": "2026-11-21",
                  "status": "待发货", "shipping": "暂无物流记录。"},
    "DEMO-2001": {"buyer": "b", "product": "cream", "channel": "live", "purchased": "2026-11-20",
                  "status": "已签收", "shipping": "模拟物流显示订单已签收。"},
}
RULES = (
    ("gift-nov-old", "gift", "品牌商城满赠", "web", None, "2026-11-01", "2026-11-15",
     "活动期间单笔实付满 299 元赠旅行装一份，以订单结算页显示为准。"),
    ("gift-nov-current", "gift", "品牌商城满赠", "web", None, "2026-11-15", "2026-12-01",
     "活动期间单笔实付满 399 元赠旅行装一份，以订单结算页显示为准。"),
    ("gift-live", "gift", "直播间满赠", "live", None, "2026-11-01", "2026-12-01",
     "活动期间直播间单笔实付满 499 元赠旅行装一份，以订单结算页显示为准。"),
    ("returns-web", "returns", "品牌商城退换处理", "web", None, "2026-11-01", None,
     "通过订单详情提交退换申请，由人工根据商品状态和订单记录核实。"),
    ("returns-live", "returns", "直播间退换处理", "live", None, "2026-11-01", None,
     "从直播间订单详情提交售后申请，由人工核实。"),
    ("cream-info", "product", "舒缓面霜规格", None, "cream", "2026-11-01", None,
     "舒缓面霜为 50 克罐装，商品详情页可查看完整配料表。"),
    ("cleanser-info", "product", "氨基酸洁面规格", None, "cleanser", "2026-11-01", None,
     "氨基酸洁面为 100 毫升软管装，商品详情页可查看完整配料表。"),
)
TOPIC_TERMS = {
    "gift": ("赠品", "满赠", "活动", "满多少", "赠送", "送旅行装", "优惠"),
    "returns": ("退货", "换货", "退款", "退换", "拆封"),
    "product": ("规格", "容量", "多少克", "多少毫升", "配料", "净含量"),
}
TOPIC_QUERIES = {"gift": "满赠", "returns": "退换处理", "product": "规格"}
ORDER_PATTERN = re.compile(r"(?<![A-Z0-9-])DEMO-\d{4}(?![A-Z0-9-])", re.ASCII)
HANDOFF_TERMS = ("过敏", "不适", "红肿", "刺痛", "呼吸困难", "起疹", "投诉", "申请退款", "申请退货",
                 "转人工", "人工客服", "找人工", "联系人工", "需要人工")


def visible_order(buyer, order_id):
    order = ORDERS.get(order_id)
    if order is None or order["buyer"] != buyer:
        return None
    return {"id": order_id, "product": PRODUCTS[order["product"]], "channel": CHANNELS[order["channel"]],
            **{key: order[key] for key in ("purchased", "status", "shipping")}}


def needs_handoff(text):
    return text.strip().rstrip("。？！?!") == "人工" or any(term in text for term in HANDOFF_TERMS)


def unavailable_order():
    return {"kind": "unavailable", "text": "当前体验身份下查不到该订单，请核对订单号。"}


@lru_cache(maxsize=12)
def _rule_view(rules):
    records = []
    for rule_id, _, title, _, _, _, _, body in rules:
        body_lines = indent(body, "    ") if "\n" in body else body
        content = f"# {title}\n{body_lines}"
        snapshot = prepare_document(content, rule_id, max_chars=max(1000, len(content)))
        records.append({"id": _snapshot_id(_payload(snapshot)), "source": rule_id,
                        "document_id": snapshot["document"]["id"], "snapshot": snapshot})
    return HierarchyView(records)


def answer_message(buyer, message, product, channel, *, rules=RULES):
    text = unicodedata.normalize("NFKC", message).strip()
    identifiers = list(dict.fromkeys(ORDER_PATTERN.findall(text.upper())))
    if len(identifiers) > 1:
        return {"kind": "clarify", "text": "请一次提供一个订单号。"}
    order_id = identifiers[0] if identifiers else None
    order = visible_order(buyer, order_id)

    # Unstructured comments can request a human, but cannot trigger refunds or order writes.
    if needs_handoff(text):
        return {"kind": "handoff", "text": "这条反馈需要人工接手。你可以提交售后工单，并补充需要客服了解的情况。",
                "order_id": order_id if order else None}
    if order_id and order is None:
        return unavailable_order()

    topics = [topic for topic, terms in TOPIC_TERMS.items() if any(term in text for term in terms)]
    if len(topics) > 1:
        return {"kind": "clarify", "text": "请先选择一项咨询：活动赠品、退换规则或商品规格。"}
    historical = any(term in text for term in ("下单时", "购买时", "买的时候"))
    shipping = any(term in text for term in ("物流", "发货", "运单", "签收", "订单状态", "到哪"))
    if shipping and topics:
        return {"kind": "clarify", "text": "请分开咨询订单物流和商品或活动规则。"}
    if historical or shipping or (order_id is not None and not topics):
        if order_id is None:
            return {"kind": "clarify", "text": "请在问题中带上你的模拟订单号。"}
        if not historical or not topics:
            return {"kind": "order", "text": f"订单 {order_id} 当前{order['status']}。{order['shipping']}", "order": order}
    if not topics:
        return {"kind": "clarify", "text": "我可以帮你查活动赠品、商品规格、退换规则或订单物流。也可以输入“转人工”。"}

    topic = topics[0]
    effective_date = order["purchased"] if historical else DEMO_DATE.isoformat()
    mentioned_products = [key for key, name in PRODUCTS.items() if name in text]
    mentioned_channels = [key for key, name in CHANNELS.items() if name in text]
    if len(mentioned_products) > 1 or len(mentioned_channels) > 1:
        return {"kind": "clarify", "text": "请一次咨询一个商品和渠道。"}
    if order:
        product = ORDERS[order_id]["product"]
        channel = ORDERS[order_id]["channel"]
        if (mentioned_products and product not in mentioned_products) or (mentioned_channels and channel not in mentioned_channels):
            return {"kind": "clarify", "text": "问题里的商品或渠道与订单记录不一致，请核对后再问。"}
    else:
        product = mentioned_products[0] if mentioned_products else product
        channel = mentioned_channels[0] if mentioned_channels else channel
    eligible = tuple(rule for rule in rules if rule[1] == topic
                     and rule[3] in (None, channel) and rule[4] in (None, product)
                     and rule[5] <= effective_date and (rule[6] is None or effective_date < rule[6]))
    if len(eligible) != 1:
        return {"kind": "handoff", "text": "未找到唯一适用的规则，可以请人工客服核实。", "order_id": order_id if order else None}
    rule_id, _, title, _, _, starts_at, ends_at, _ = eligible[0]
    hit = _rule_view(eligible).search(TOPIC_QUERIES[topic], top_k=1)
    if not hit:
        return {"kind": "handoff", "text": "暂时取不到规则原文，可以请人工客服核实。", "order_id": order_id if order else None}
    excerpt = dedent(hit[0]["text"].partition("\n")[2])
    return {"kind": "policy", "text": excerpt, "citation": {"title": title, "rule_id": rule_id,
            "start": starts_at, "end": ends_at, "effective_date": effective_date,
            "snapshot_id": hit[0]["snapshot_id"], "chunk_id": hit[0]["id"], "excerpt": excerpt}}


AFTER_SALE_LABELS = {"submitted": "申请已提交", "reviewing": "人工审核中", "completed": "处理完成"}
ORDER_FOLLOW_UP = re.compile(r"^(那|这单|该订单|这个订单)")
PROGRESS_TERMS = ("售后进度", "申请进度", "退款进度", "退货进度")


def conversation_turn(buyer, message, product, channel, state, rules, after_sales):
    """Resolve carried order/question context without trusting client-side IDs."""
    text = unicodedata.normalize("NFKC", message).strip()
    identifiers = list(dict.fromkeys(ORDER_PATTERN.findall(text.upper())))
    explicit = identifiers[0] if len(identifiers) == 1 else None
    owned = explicit if visible_order(buyer, explicit) else None
    last_order = state.get("order_id") if not identifiers else owned
    request = text
    if owned and state.get("pending") and text.upper().rstrip("。？！?!") == explicit:
        request = f"{state['pending']} {owned}"
    elif last_order and not identifiers and (ORDER_FOLLOW_UP.match(text) or any(
        word in text for word in ("下单时", "购买时", "买的时候", "物流", "发货", *PROGRESS_TERMS)
    )):
        request = f"{text} {last_order}"

    if any(word in text for word in ("能退", "可以退", "能不能退")):
        request += " 退换"
    uses_progress = any(word in request for word in PROGRESS_TERMS)
    if uses_progress and len(identifiers) <= 1 and not needs_handoff(request):
        if last_order is None:
            reply = unavailable_order() if explicit else {
                "kind": "clarify", "text": "请在问题中带上你的模拟订单号。"}
        else:
            item = after_sales.get(last_order)
            reply = {"kind": "after_sale", "text": (
                f"订单 {last_order} 的售后状态为{AFTER_SALE_LABELS[item['status']]}。" if item else
                f"订单 {last_order} 暂无售后申请。"), "after_sale": item}
    else:
        reply = answer_message(buyer, request, product, channel, rules=rules)

    next_state = {"order_id": last_order}
    if reply["kind"] == "clarify" and reply["text"] == "请在问题中带上你的模拟订单号。":
        next_state["pending"] = request
    elif state.get("pending") and explicit and not owned and reply["kind"] == "unavailable":
        next_state["pending"] = state["pending"]
    return reply, next_state


class SupportStore:
    """Persist demo sessions and handoffs outside the public output directory."""

    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, buyer TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    request_id TEXT NOT NULL, order_id TEXT, summary TEXT NOT NULL,
                    status TEXT NOT NULL, resolution TEXT, created_at TEXT NOT NULL,
                    UNIQUE (session_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS conversation_state (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id), state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS support_messages (
                    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    query TEXT, answer TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS support_messages_session ON support_messages(session_id, id);
                CREATE TABLE IF NOT EXISTS support_staff (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS ticket_claims (
                    ticket_id TEXT PRIMARY KEY REFERENCES tickets(id), staff_id TEXT NOT NULL REFERENCES support_staff(id)
                );
                CREATE TABLE IF NOT EXISTS support_after_sales (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    order_id TEXT NOT NULL, ticket_id TEXT UNIQUE NOT NULL REFERENCES tickets(id),
                    UNIQUE (session_id, order_id)
                );
                CREATE TABLE IF NOT EXISTS support_rule_versions (
                    id TEXT PRIMARY KEY, base_rule_id TEXT UNIQUE NOT NULL,
                    starts_at TEXT NOT NULL, ends_at TEXT, body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS support_rule_drafts (
                    id TEXT PRIMARY KEY, base_rule_id TEXT NOT NULL, starts_at TEXT NOT NULL,
                    ends_at TEXT, body TEXT NOT NULL, published_id TEXT REFERENCES support_rule_versions(id)
                );
                CREATE TABLE IF NOT EXISTS support_quality_runs (
                    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, report TEXT NOT NULL
                );
            """)
            with db:
                yield db

    @staticmethod
    def session_id(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_session(self, buyer):
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("INSERT INTO sessions VALUES (?, ?)", (self.session_id(token), buyer))
        return token

    def buyer_for(self, token):
        with self.connect() as db:
            row = db.execute("SELECT buyer FROM sessions WHERE id = ?", (self.session_id(token),)).fetchone()
        return row["buyer"] if row else None

    @staticmethod
    def _tickets(db, session_id):
        return [dict(row) for row in db.execute("""
            SELECT id, order_id, summary, status, resolution, created_at FROM tickets
            WHERE session_id = ? ORDER BY created_at DESC, id DESC
        """, (session_id,))]

    @staticmethod
    def _messages(db, session_id):
        rows = db.execute("""SELECT * FROM (
            SELECT id, query, answer FROM support_messages WHERE session_id = ? ORDER BY id DESC LIMIT 80
        ) ORDER BY id""", (session_id,))
        return [{"id": row["id"], "query": row["query"], "answer": json.loads(row["answer"])} for row in rows]

    @staticmethod
    def _after_sales(db, session_id):
        rows = db.execute("""
            SELECT sale.id, sale.order_id, sale.ticket_id, ticket.summary, ticket.status, ticket.resolution
            FROM support_after_sales sale JOIN tickets ticket ON ticket.id = sale.ticket_id
            WHERE sale.session_id = ? ORDER BY ticket.created_at DESC
        """, (session_id,))
        statuses = {"pending": "submitted", "in_progress": "reviewing", "resolved": "completed"}
        return {row["order_id"]: {**dict(row), "status": statuses[row["status"]]} for row in rows}

    def conversation(self, token):
        session_id = self.session_id(token)
        with self.connect() as db:
            db.execute("BEGIN")
            return {"messages": self._messages(db, session_id), "tickets": self._tickets(db, session_id),
                    "after_sales": list(self._after_sales(db, session_id).values())}

    def ask(self, token, buyer, message, product, channel):
        session_id = self.session_id(token)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("""
                SELECT tickets.id FROM tickets JOIN ticket_claims ON ticket_claims.ticket_id = tickets.id
                WHERE session_id = ? AND status = 'in_progress'
            """, (session_id,)).fetchone()
            row = db.execute("SELECT state FROM conversation_state WHERE session_id = ?", (session_id,)).fetchone()
            state = json.loads(row["state"]) if row else {}
            progress = any(term in message for term in PROGRESS_TERMS)
            order_only = ORDER_PATTERN.fullmatch(unicodedata.normalize("NFKC", message).upper().rstrip("。？！?!"))
            pending_progress = order_only and any(term in state.get("pending", "") for term in PROGRESS_TERMS)
            if active and (not (progress or pending_progress) or needs_handoff(message)):
                answer = {"kind": "human_pending", "text": "消息已交给人工客服。", "ticket_id": active["id"]}
            else:
                answer, state = conversation_turn(buyer, message, product, channel, state,
                                                   self._rules(db), self._after_sales(db, session_id))
                db.execute("""INSERT INTO conversation_state VALUES (?, ?)
                    ON CONFLICT (session_id) DO UPDATE SET state = excluded.state
                """, (session_id, json.dumps(state)))
            message_row = db.execute("INSERT INTO support_messages (session_id, query, answer) VALUES (?, ?, ?)",
                                     (session_id, message, json.dumps(answer)))
            return {**answer, "message_id": message_row.lastrowid}

    def submit_ticket(self, token, request_id, order_id, summary):
        session_id = self.session_id(token)
        with self.connect() as db:
            db.execute("""
                INSERT INTO tickets (id, session_id, request_id, order_id, summary, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?) ON CONFLICT (session_id, request_id) DO NOTHING
            """, (uuid4().hex, session_id, request_id, order_id, summary, datetime.now(timezone.utc).isoformat()))
            row = db.execute("""
                SELECT id, order_id, summary, status, resolution, created_at FROM tickets
                WHERE session_id = ? AND request_id = ?
            """, (session_id, request_id)).fetchone()
            if row["summary"] != summary or row["order_id"] != order_id:
                raise ValueError("该提交编号已用于另一份工单。")
            return dict(row)

    def apply_after_sale(self, token, order_id, summary):
        session_id = self.session_id(token)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = self._after_sales(db, session_id).get(order_id)
            if existing:
                return existing
            sale_id, ticket_id = uuid4().hex, uuid4().hex
            db.execute("INSERT INTO tickets VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?)",
                       (ticket_id, session_id, f"after-sale-{sale_id}", order_id, summary, datetime.now(timezone.utc).isoformat()))
            db.execute("INSERT INTO support_after_sales VALUES (?, ?, ?, ?)", (sale_id, session_id, order_id, ticket_id))
            return self._after_sales(db, session_id)[order_id]

    def start_staff(self):
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("INSERT INTO support_staff VALUES (?)", (self.session_id(token),))
        return token

    def staff_id(self, token):
        identifier = self.session_id(token)
        with self.connect() as db:
            return identifier if db.execute("SELECT id FROM support_staff WHERE id = ?", (identifier,)).fetchone() else None

    def queue(self, staff_id):
        with self.connect() as db:
            rows = db.execute("""
                SELECT t.id, t.summary, t.order_id, t.status, t.created_at, s.buyer, c.staff_id, a.id AS after_sale_id
                FROM tickets t JOIN sessions s ON s.id = t.session_id
                LEFT JOIN ticket_claims c ON c.ticket_id = t.id
                LEFT JOIN support_after_sales a ON a.ticket_id = t.id
                WHERE c.staff_id IS NULL OR c.staff_id = ?
                ORDER BY CASE t.status WHEN 'in_progress' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, t.created_at DESC
                LIMIT 100
            """, (staff_id,))
            return [{key: row[key] for key in row.keys() if key != "staff_id"} for row in rows]

    @staticmethod
    def _ticket(db, ticket_id, staff_id):
        row = db.execute("""
            SELECT t.*, c.staff_id FROM tickets t LEFT JOIN ticket_claims c ON t.id = c.ticket_id WHERE t.id = ?
        """, (ticket_id,)).fetchone()
        if not row or row["staff_id"] not in (None, staff_id):
            raise LookupError("工单不存在或已由其他客服接手。")
        return row

    def ticket_detail(self, ticket_id, staff_id):
        with self.connect() as db:
            db.execute("BEGIN")
            row = self._ticket(db, ticket_id, staff_id)
            buyer = db.execute("SELECT buyer FROM sessions WHERE id = ?", (row["session_id"],)).fetchone()["buyer"]
            messages = self._messages(db, row["session_id"])
            citation = next((item["answer"]["citation"] for item in reversed(messages) if item["answer"].get("citation")), None)
            return {key: row[key] for key in ("id", "summary", "status", "resolution", "created_at")} | {
                "buyer": buyer, "order": visible_order(buyer, row["order_id"]), "messages": messages,
                "citation": citation, "assigned": row["staff_id"] is not None}

    def act_on_ticket(self, ticket_id, staff_id, action, text=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ticket = self._ticket(db, ticket_id, staff_id)
            session_id = ticket["session_id"]
            if action == "claim":
                if ticket["status"] == "resolved" or db.execute("""
                    SELECT 1 FROM tickets t JOIN ticket_claims c ON t.id = c.ticket_id
                    WHERE t.session_id = ? AND t.id <> ? AND t.status = 'in_progress'
                """, (session_id, ticket_id)).fetchone():
                    raise ValueError("这张工单已结单，或该会话已有客服接手。")
                db.execute("INSERT OR IGNORE INTO ticket_claims VALUES (?, ?)", (ticket_id, staff_id))
                db.execute("UPDATE tickets SET status = 'in_progress' WHERE id = ?", (ticket_id,))
                return
            if ticket["staff_id"] != staff_id or ticket["status"] != "in_progress":
                raise ValueError("请先受理这张工单。")
            if action == "resolve":
                db.execute("UPDATE tickets SET status = 'resolved', resolution = ? WHERE id = ?", (text, ticket_id))
            answer = {"kind": "human", "text": text, "ticket_id": ticket_id, "resolved": action == "resolve"}
            db.execute("INSERT INTO support_messages (session_id, query, answer) VALUES (?, NULL, ?)",
                       (session_id, json.dumps(answer)))

    @staticmethod
    def _rules(db):
        rules = {rule[0]: rule for rule in RULES}
        for row in db.execute("SELECT * FROM support_rule_versions ORDER BY rowid"):
            base = rules[row["base_rule_id"]]
            end = min(base[6], row["starts_at"]) if base[6] else row["starts_at"]
            rules[base[0]] = (*base[:6], end, base[7])
            rules[row["id"]] = (row["id"], *base[1:5], row["starts_at"], row["ends_at"], row["body"])
        return tuple(rules.values())

    def rules(self):
        with self.connect() as db:
            return self._rules(db)

    @staticmethod
    def _rule_dict(rule):
        keys = ("id", "topic", "title", "channel", "product", "starts_at", "ends_at", "body")
        return dict(zip(keys, rule))

    def rule_catalog(self):
        with self.connect() as db:
            db.execute("BEGIN")
            successors = {row["base_rule_id"] for row in db.execute("SELECT base_rule_id FROM support_rule_versions")}
            rules = self._rules(db)
            # Seed versions already have a later sibling in the same scope.
            latest_by_scope = {}
            for rule in sorted(rules, key=lambda row: row[5]):
                latest_by_scope[(rule[1], rule[3], rule[4])] = rule[0]
            return {"demo_date": DEMO_DATE.isoformat(),
                    "versions": [{**self._rule_dict(rule), "replaceable": rule[0] in latest_by_scope.values() and rule[0] not in successors}
                                 for rule in reversed(rules)],
                    "drafts": [dict(row) for row in db.execute("SELECT * FROM support_rule_drafts ORDER BY rowid DESC")]}

    @staticmethod
    def _validate_rule_change(rules, base_id, starts_at, ends_at):
        base = next((rule for rule in rules if rule[0] == base_id), None)
        if base is None:
            raise LookupError("找不到要更新的规则版本。")
        scope = (base[1], base[3], base[4])
        if any((rule[1], rule[3], rule[4]) == scope and rule[5] > base[5] for rule in rules):
            raise ValueError("该规则已有更新版本，请基于最新版本建立草稿。")
        # Existing orders retain the effective rules of their purchase dates.
        if starts_at <= base[5] or starts_at < DEMO_DATE.isoformat() or (ends_at and ends_at <= starts_at):
            raise ValueError("生效日期须晚于上版开始日期且不早于演示日期，失效日期须晚于生效日期。")
        return base

    def save_rule_draft(self, base_id, starts_at, ends_at, body, draft_id=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if draft_id:
                previous = db.execute("SELECT * FROM support_rule_drafts WHERE id = ?", (draft_id,)).fetchone()
                if previous is None:
                    raise LookupError("找不到该规则草稿。")
                if previous["published_id"] or previous["base_rule_id"] != base_id:
                    raise ValueError("该草稿已发布，或拟更新的规则版本不匹配。")
            self._validate_rule_change(self._rules(db), base_id, starts_at, ends_at)
            if draft_id:
                db.execute("UPDATE support_rule_drafts SET starts_at = ?, ends_at = ?, body = ? WHERE id = ?",
                           (starts_at, ends_at, body, draft_id))
            else:
                draft_id = uuid4().hex
                db.execute("INSERT INTO support_rule_drafts VALUES (?, ?, ?, ?, ?, NULL)",
                           (draft_id, base_id, starts_at, ends_at, body))
        return draft_id

    def preview_rule(self, draft_id):
        with self.connect() as db:
            db.execute("BEGIN")
            draft = db.execute("SELECT * FROM support_rule_drafts WHERE id = ?", (draft_id,)).fetchone()
            if draft is None:
                raise LookupError("找不到该规则草稿。")
            rules = self._rules(db)
            base = next(rule for rule in rules if rule[0] == draft["base_rule_id"])
            return {"draft": dict(draft), "draft_sha256": self._draft_hash(draft), "previous": self._rule_dict(base),
                    "published": next((self._rule_dict(rule) for rule in rules if rule[0] == draft["published_id"]), None)}

    @staticmethod
    def _draft_hash(draft):
        content = [draft[key] for key in ("id", "base_rule_id", "starts_at", "ends_at", "body")]
        return hashlib.sha256(json.dumps(content, ensure_ascii=False).encode("utf-8")).hexdigest()

    def publish_rule(self, draft_id, reviewed_hash):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            draft = db.execute("SELECT * FROM support_rule_drafts WHERE id = ?", (draft_id,)).fetchone()
            if draft is None:
                raise LookupError("找不到该规则草稿。")
            if self._draft_hash(draft) != reviewed_hash:
                raise ValueError("草稿有更新，请重新预览后发布。")
            if draft["published_id"]:
                return draft["published_id"]
            self._validate_rule_change(self._rules(db), draft["base_rule_id"], draft["starts_at"], draft["ends_at"])
            rule_id = f"policy-{uuid4().hex}"
            db.execute("INSERT INTO support_rule_versions VALUES (?, ?, ?, ?, ?)",
                       (rule_id, draft["base_rule_id"], draft["starts_at"], draft["ends_at"], draft["body"]))
            db.execute("UPDATE support_rule_drafts SET published_id = ? WHERE id = ?", (rule_id, draft_id))
            return rule_id

    def record_quality(self, report):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT report FROM support_quality_runs ORDER BY id DESC LIMIT 1").fetchone()
            previous_cases = {item["id"]: item["passed"] for item in json.loads(previous["report"])["cases"]} if previous else {}
            report["regressed"] = [item["id"] for item in report["cases"] if previous_cases.get(item["id"]) and not item["passed"]]
            report["recovered"] = [item["id"] for item in report["cases"] if previous_cases.get(item["id"]) is False and item["passed"]]
            report["created_at"] = datetime.now(timezone.utc).isoformat()
            db.execute("INSERT INTO support_quality_runs (created_at, report) VALUES (?, ?)",
                       (report["created_at"], json.dumps(report)))
            return report

    def quality_history(self):
        with self.connect() as db:
            return [json.loads(row["report"]) for row in db.execute("SELECT report FROM support_quality_runs ORDER BY id DESC LIMIT 10")]
