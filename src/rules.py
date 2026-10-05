"""事件流的领域规则校验。

每条规则接收按发生时间排序的事件流，返回违规描述列表（空列表表示通过）。
规则编号与 contracts/event-catalog.md 中的 R2–R6 对应；R1 守恒见 ledger.py。
"""

ADJUSTMENT = "LATE_STATEMENT_ADJUSTED"


def check_closed_period(events: list[dict]) -> list[str]:
    """R4：已封账批次只接受差额调整，且调整必须入账到未封账批次。"""
    opened: set[str] = set()
    closed: set[str] = set()
    errors: list[str] = []
    for event in events:
        payload = event.get("payload", {})
        event_type = event["event_type"]
        if event_type == "SETTLEMENT_BATCH_OPENED":
            opened.add(event["aggregate_id"])
            continue
        if event_type == "SETTLEMENT_CLOSED":
            closed.add(event["aggregate_id"])
            continue
        batch_id = payload.get("settlement_batch_id")
        if batch_id is None:
            continue
        if batch_id not in opened:
            errors.append(f"{event['event_id']}：批次 {batch_id} 未开启")
        if event_type == ADJUSTMENT:
            if payload.get("adjusts_batch_id") not in closed:
                errors.append(f"{event['event_id']}：差额调整必须指向已封账批次")
            if batch_id in closed:
                errors.append(f"{event['event_id']}：差额调整不得入账到已封账批次 {batch_id}")
        elif batch_id in closed:
            errors.append(f"{event['event_id']}：批次 {batch_id} 已封账，仅接受差额调整")
    return errors


def check_no_double_pay(events: list[dict]) -> list[str]:
    """R3：同一笔消费只确认一次；复用素材的消费不再按次计酬。"""
    reused = {
        event["payload"].get("consumption_id", event["aggregate_id"])
        for event in events
        if event["event_type"] == "CONSUMPTION_RECORDED" and event["payload"].get("reuse_of")
    }
    seen: set[str] = set()
    errors: list[str] = []
    for event in events:
        if event["event_type"] != "REVENUE_RECOGNIZED":
            continue
        consumption_id = event["payload"].get("consumption_id")
        if consumption_id is None:
            continue
        if consumption_id in reused:
            errors.append(f"{event['event_id']}：复用素材消费 {consumption_id} 不得重复计酬")
        if consumption_id in seen:
            errors.append(f"{event['event_id']}：消费 {consumption_id} 已被确认过")
        seen.add(consumption_id)
    return errors


def check_deferred_cap(events: list[dict]) -> list[str]:
    """R2：同一笔预付的累计确认与退款不得超过其递延总额。"""
    deferred: dict[str, int] = {}
    used: dict[str, int] = {}
    for event in events:
        payload = event.get("payload", {})
        event_type = event["event_type"]
        if event_type == "REVENUE_DEFERRED":
            deferred[payload["payment_id"]] = payload["amount"]
        elif event_type == "REVENUE_RECOGNIZED" and payload.get("deferred_from"):
            key = payload["deferred_from"]
            used[key] = used.get(key, 0) + payload["amount"]
        elif event_type in ("REFUND_RECORDED", "CHARGEBACK_RECORDED") and payload.get("deferred_from"):
            key = payload["deferred_from"]
            used[key] = used.get(key, 0) + payload["amount"]
    return [
        f"支付 {payment_id} 累计确认/退款 {total} 超出递延 {deferred[payment_id]}"
        for payment_id, total in used.items()
        if payment_id in deferred and total > deferred[payment_id]
    ]


def check_escrow_scope(events: list[dict]) -> list[str]:
    """R5：争议暂存只涉及争议当事方，结案前不得新增暂存。"""
    disputes: dict[str, dict] = {}
    errors: list[str] = []
    for event in events:
        payload = event.get("payload", {})
        event_type = event["event_type"]
        if event_type == "DISPUTE_OPENED":
            disputes[event["aggregate_id"]] = {
                "parties": set(payload.get("parties", [])),
                "resolved": False,
            }
        elif event_type == "DISPUTE_RESOLVED":
            disputes.setdefault(event["aggregate_id"], {"parties": set(), "resolved": False})[
                "resolved"
            ] = True
        elif event_type in ("ESCROW_HELD", "ESCROW_RELEASED"):
            dispute = disputes.get(payload.get("dispute_id"))
            if dispute is None:
                errors.append(f"{event['event_id']}：暂存指向未登记的争议")
            elif event_type == "ESCROW_HELD" and dispute["resolved"]:
                errors.append(f"{event['event_id']}：争议已结案，不得再暂存")
            elif event_type == "ESCROW_HELD" and payload.get("contributor_id") not in dispute["parties"]:
                errors.append(
                    f"{event['event_id']}：暂存涉及非当事方 {payload.get('contributor_id')}，"
                    "不得拖住无争议贡献者"
                )
    return errors


def check_rights_constraint(events: list[dict]) -> list[str]:
    """R6：权利约束生效后，受限权利项下不得再产生分账。"""
    constraints: list[tuple[str, str]] = []
    errors: list[str] = []
    for event in events:
        payload = event.get("payload", {})
        if event["event_type"] == "RIGHTS_SCOPE_CONSTRAINED":
            for term_id in payload.get("rights_term_ids", []):
                constraints.append((term_id, payload["effective_from"]))
        elif event["event_type"] == "REVENUE_ALLOCATED":
            term_id = payload.get("rights_term_id")
            for constrained_id, effective_from in constraints:
                if term_id == constrained_id and event["occurred_at"] >= effective_from:
                    errors.append(
                        f"{event['event_id']}：权利 {term_id} 已受约束，分账超出其有效范围"
                    )
    return errors
