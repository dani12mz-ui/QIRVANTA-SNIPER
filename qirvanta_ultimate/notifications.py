"""Local Bulgarian notifications with explicit delivery boundaries."""
from decimal import Decimal


LABELS = {
    "BUY_CANDIDATE": "Кандидат за покупка",
    "SELL_CANDIDATE": "Кандидат за продажба",
    "NO_TRADE": "Няма допуснат сигнал",
    "WAIT_CONFIRMATION": "Изчаква потвърждение",
    "SIGNAL_EXPIRED": "Срокът на сигнала изтече",
    "INVALIDATED": "Идеята е невалидна",
    "DATA_UNAVAILABLE": "Липсват актуални данни",
    "RISK_BLOCKED": "Риск правилото блокира плана",
}


def format_price(price, tick_size):
    tick = Decimal(str(tick_size)).normalize()
    return format(Decimal(str(price)), f".{max(0, -tick.as_tuple().exponent)}f")


def notification_text(decision, tick_size):
    state = decision["state"]
    headline = f"{decision['symbol']} · {LABELS[state]} · {decision['reason']}"
    if state not in ("BUY_CANDIDATE", "SELL_CANDIDATE"):
        return headline
    plan = decision["specialists"]["management"]["facts"]
    fmt = lambda p: format_price(p, tick_size)
    targets = " / ".join(fmt(t) for t in plan["targets"])
    return (f"{headline}\nПредложен план {plan['plan_id'][:12]}\n"
            f"Вход {fmt(plan['entry'])} | SL {fmt(plan['stop'])} | TP1–3 {targets}")


def record_notification(journal, state, decision, tick_size):
    marker = [decision["state"], decision["reason"]]
    if decision["state"] in ("BUY_CANDIDATE", "SELL_CANDIDATE"):
        marker.append(decision["specialists"]["management"]["facts"]["plan_id"])
    if state.get("last_notification") == marker:
        return
    state["last_notification"] = marker
    journal.append("LOCAL_NOTIFICATION", decision["as_of_ms"],
                   {"trace_id": decision["trace_id"], "delivery": "PERSISTED_LOCAL",
                    "provider": "SQLITE", "text": notification_text(decision, tick_size)})


def record_paper_fill(journal, position, at_ms, reason, price, quantity):
    label = {"ENTRY": "Симулиран вход", "STOP": "Симулиран стоп", "TIME_STOP": "Симулиран изход по срок"}.get(
        reason, f"Симулиран {reason}")
    value = format_price(price, position["costs"]["tick_size"])
    text = f"{label} · план {position['plan_id'][:12]} · цена {value} · количество {quantity:g}"
    journal.append("LOCAL_NOTIFICATION", at_ms,
                   {"trace_id": position["trace_id"], "delivery": "PERSISTED_LOCAL",
                    "provider": "SQLITE", "text": text})
