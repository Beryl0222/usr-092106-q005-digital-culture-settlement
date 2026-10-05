"""按结算批次重放分录并验证守恒。

守恒式（按币种分别成立）：

    确认收入 + 差额调整 + 暂存释放 − 退款拒付冲减
        = 分账应付 + 争议暂存 + 各项扣减

事件只通过 payload.settlement_batch_id 归属批次。退款拒付只有冲减已确认
收入（payload.reverses_recognition 指向确认事件）时才进入批次账；未消费
递延的退款只影响递延余额，不影响批次守恒。
"""

INFLOWS = {
    "REVENUE_RECOGNIZED": "recognized",
    "LATE_STATEMENT_ADJUSTED": "adjustments",
    "ESCROW_RELEASED": "escrow_released",
}
OUTFLOWS = {
    "REVENUE_ALLOCATED": "allocated",
    "ESCROW_HELD": "escrow_held",
    "DEDUCTION_APPLIED": "deducted",
}
REVERSALS = {"REFUND_RECORDED", "CHARGEBACK_RECORDED"}

CATEGORIES = (
    "recognized",
    "adjustments",
    "escrow_released",
    "reversals",
    "allocated",
    "escrow_held",
    "deducted",
)


def replay_batch(events: list[dict], batch_id: str) -> dict:
    """重放一个结算批次的全部入账事件，按币种汇总各类金额。"""
    books: dict[str, dict[str, int]] = {}
    for event in events:
        payload = event.get("payload", {})
        if payload.get("settlement_batch_id") != batch_id:
            continue
        event_type = event["event_type"]
        amount = payload.get("amount", 0)
        currency = payload.get("currency", "CNY")
        totals = books.setdefault(currency, dict.fromkeys(CATEGORIES, 0))
        if event_type in INFLOWS:
            totals[INFLOWS[event_type]] += amount
        elif event_type in OUTFLOWS:
            totals[OUTFLOWS[event_type]] += amount
        elif event_type in REVERSALS and payload.get("reverses_recognition"):
            totals["reversals"] += amount
    return books


def conservation_gap(books: dict) -> dict:
    """返回每个币种的守恒差额；全部为 0 时批次守恒。"""
    return {
        currency: totals["recognized"] + totals["adjustments"] + totals["escrow_released"]
        - totals["reversals"]
        - totals["allocated"]
        - totals["escrow_held"]
        - totals["deducted"]
        for currency, totals in books.items()
    }
