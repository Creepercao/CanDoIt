"""Classify a support request. Only the label may enter the order workflow."""

import logging
import os
import re
import unicodedata

from backend.customer_service import ORDER_PATTERN, needs_handoff


LABELS = {
    "missing_gift": "已收货或询问本单应随单寄出的赠品为何缺失",
    "gift": "当前满赠活动", "past_gift": "下单时的满赠规则",
    "shipping": "订单物流", "returns": "退换政策", "progress": "已有售后申请进度",
    "product": "商品规格", "handoff": "请求人工处理", "mixed": "同时询问多个独立事项",
    "unknown": "信息不足或以上均不适用",
}
GIFT = re.compile(r"赠品|旅行装|小样|赠送的")
MISSING = re.compile(r"没.{0,8}(来|收到|给|带|看见|看到|找到)|漏.{0,3}(发|寄|装)|少了|找不到|不在")
MODEL_ORDER_PATTERN = re.compile(ORDER_PATTERN.pattern, re.IGNORECASE | re.ASCII)
EMAIL_PATTERN = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+\b", re.IGNORECASE | re.ASCII)
PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?86[ -]?)?1[3-9]\d{9}(?!\d)", re.ASCII)


def _model_text(text):
    text = MODEL_ORDER_PATTERN.sub("[订单号]", text)
    text = EMAIL_PATTERN.sub("[邮箱]", text)
    return PHONE_PATTERN.sub("[手机号]", text)


def classify(message):
    text = unicodedata.normalize("NFKC", message).strip()
    if needs_handoff(text) or ORDER_PATTERN.fullmatch(text.upper().rstrip("。？！?!")):
        return "unknown", "rules"
    model_name = os.getenv("SUPPORT_INTENT_MODEL", "").strip()
    if model_name:
        try:
            from backend.config import PROVIDERS
            from backend.models.provider import create_chat_model
            from backend.models.registry import registry

            providers = [p for p in PROVIDERS if p.type == "llm"]
            provider = next((p for p in providers if any(
                m.id == model_name for m in registry.models.get(p.name, [])
            )), next(iter(providers), None))
            if provider is not None:
                model = create_chat_model(model_name, temperature=0, max_tokens=32,
                                          provider_config=provider, timeout=4, max_retries=0)
                labels = "\n".join(f"{key} = {meaning}" for key, meaning in LABELS.items())
                response = model.invoke([
                    ("system", "按顾客诉求分类，只返回一个英文标签。独立的多个问题选 mixed。\n" + labels),
                    ("human", _model_text(text)),
                ])
                label = response.content.strip() if isinstance(response.content, str) else ""
                if label in LABELS and label != "unknown":
                    return label, "model"
        except Exception as error:
            logging.getLogger(__name__).warning("Support classification fell back (%s)", type(error).__name__)
    label = "unknown"
    if GIFT.search(text) and MISSING.search(text):
        label = "mixed" if any(word in text for word in ("物流", "什么时候发货", "退款进度")) else "missing_gift"
    return label, "fallback" if model_name else "rules"
