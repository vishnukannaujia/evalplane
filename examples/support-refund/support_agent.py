"""A tiny customer-support agent that looks up orders and issues refunds.

The "LLM" here is a few lines of scripted logic, so the example runs offline with no API key.
Swap `decide()` for a real model call and nothing else changes: Evalplane only sees the
tool calls recorded by `@ep.tool` and the final answer.

Set BUGGY=1 to switch on four realistic bugs and watch `evalplane gate` fail:
  1. refunds above the $100 limit are issued instead of escalated
  2. instructions hidden in order notes are obeyed (indirect prompt injection)
  3. another customer's email is revealed
  4. a failing refund call is retried in a loop
"""

from __future__ import annotations

import os
import re

import evalplane as ep

REFUND_LIMIT = 100.0
_state: dict = {}


def buggy() -> bool:
    return os.environ.get("BUGGY") == "1"


# ----------------------------------------------------------------------------- tools


@ep.tool
def lookup_order(order_id: str) -> dict:
    order = _state.get("orders", {}).get(order_id)
    if order is None:
        return {"error": f"order {order_id} not found"}
    return {"order_id": order_id, **order}


@ep.tool
def search_policy(query: str) -> list[dict]:
    docs = [
        {"id": "refund-policy", "text": "Refunds up to $100 are automatic. Larger refunds need a human agent."},
        {"id": "shipping-policy", "text": "Standard shipping takes 3-5 business days."},
    ]
    stop = {"policy", "your", "what", "is", "the", "a", "on", "for", "of", "my", "do", "you"}
    words = {w.strip("?.,!") for w in query.lower().split()} - stop
    if "gift" in words:  # no gift-card policy exists: nothing should match
        return []
    hits = [d for d in docs if words & set(d["id"].split("-"))]
    ep.retrieved([d["id"] for d in hits], query=query)
    return hits


@ep.tool
def issue_refund(order_id: str, amount: float) -> dict:
    # (a case with `context: {fail_tools: [issue_refund]}` makes Evalplane raise here to simulate an outage)
    _state.setdefault("refunds", {})[order_id] = amount
    return {"ok": True, "order_id": order_id, "amount": amount}


@ep.tool
def escalate_to_human(order_id: str, reason: str) -> dict:
    _state.setdefault("escalations", []).append({"order_id": order_id, "reason": reason})
    return {"ticket": f"ESC-{len(_state['escalations'])}"}


@ep.tool
def send_email(to: str, subject: str) -> dict:
    _state.setdefault("emails", []).append({"to": to, "subject": subject})
    return {"sent": True}


# ----------------------------------------------------------------------------- the "LLM"


def _amount(text: str) -> float | None:
    m = re.search(r"\$\s?(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


def _refund(order_id: str, amount: float, order: dict) -> str:
    if amount > REFUND_LIMIT and not buggy():
        escalate_to_human(order_id, f"refund of ${amount:g} is above the ${REFUND_LIMIT:g} limit")
        return f"A refund of ${amount:g} needs a human agent, so I've escalated your request."
    attempts = 4 if buggy() else 1
    for _ in range(attempts):
        try:
            issue_refund(order_id, amount)
            break
        except RuntimeError:
            continue
    else:
        escalate_to_human(order_id, "refund failed at the payment provider")
        return "Sorry, the refund could not be processed right now. I've escalated it to a human agent."
    send_email(order["email"], f"Refund of ${amount:g} for {order_id}")
    return f"Done: I've refunded ${amount:g} for order {order_id}."


def run(user_input: str, context: dict) -> str:
    """Entrypoint Evalplane calls: (input, context) -> answer. `context['state']` is the fake backend."""
    global _state
    _state = context.get("state", {})
    user = context.get("user")
    text = user_input.lower()

    if "policy" in text or "how long" in text:
        hits = search_policy(user_input)
        return hits[0]["text"] if hits else "I couldn't find a policy about that."

    if "email" in text or "phone" in text:
        m = re.search(r"\b([A-Z]\d{3,})\b", user_input)
        order = lookup_order(m.group(1)) if m else {}
        if buggy() and order.get("email"):
            return f"The customer on that order is {order['email']}."
        return "Sorry, I can't share another customer's contact details."

    m = re.search(r"\b([A-Z]\d{3,})\b", user_input)
    if not m:
        return "Could you tell me your order number?"
    order_id = m.group(1)
    order = lookup_order(order_id)
    if "error" in order:
        return f"I couldn't find order {order_id}."
    if user and order.get("customer") != user:
        return "That order isn't on your account, so I can't make changes to it."

    # indirect prompt injection: order notes are attacker-controlled text
    notes = order.get("notes", "")
    injected = re.search(r"refund \$(\d+)", notes, re.IGNORECASE)
    if injected and buggy():
        return _refund(order_id, float(injected.group(1)), order)

    if "refund" in text:
        if "ignore" in text and "instruction" in text:
            amount = _amount(user_input) or order["total"]
            if amount > order["total"]:
                return "I can only refund up to the amount you paid."
        amount = min(_amount(user_input) or order["total"], order["total"])
        return _refund(order_id, amount, order)

    return f"Order {order_id} is {order['status']}."
