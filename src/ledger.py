"""整批分录重放与守恒校验。

事件不可变：重放只产生状态，不修改事件。封账批次冻结，迟到单据只能
经由 ADJUSTMENT_POSTED 进入新批次（见 docs/domain.md 不变量 1、2）。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable


def D(value: str) -> Decimal:
    return Decimal(value)


class LedgerError(Exception):
    """分录或封账不变量被破坏。"""


@dataclass
class Batch:
    aggregate_id: str
    closed_at: str | None = None
    effects: list[dict] = field(default_factory=list)


@dataclass
class Ledger:
    # account -> currency -> 贷方累计 - 借方累计
    balances: dict[str, dict[str, Decimal]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(lambda: D("0"))))
    # contributor -> currency -> 应付余额（贷-借）
    payable: dict[str, dict[str, Decimal]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(lambda: D("0"))))
    batches: dict[str, Batch] = field(default_factory=dict)
    posted: int = 0

    def trial_balance(self) -> dict[str, dict[str, Decimal]]:
        """返回各币种 借方合计 / 贷方合计。重放任意事件集合都应相等。"""
        debits: dict[str, Decimal] = defaultdict(lambda: D("0"))
        credits: dict[str, Decimal] = defaultdict(lambda: D("0"))
        for acct, by_cur in self.balances.items():
            for cur, net in by_cur.items():
                if net >= 0:
                    credits[cur] += net
                else:
                    debits[cur] += -net
        return {
            cur: {"debits": debits[cur], "credits": credits[cur]}
            for cur in set(debits) | set(credits)
        }


def _links(event: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for link in event.get("links", []):
        # 同一 rel 若有多个目标，调用方需要时用 linkmap_all；这里保留首条并支持多值
        out.setdefault(link["rel"], link["ref"])
    return out


def linkmap_all(event: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for link in event.get("links", []):
        out[link["rel"]].append(link["ref"])
    return out


def _check_event_balance(event: dict) -> None:
    totals: dict[str, dict[str, Decimal]] = defaultdict(lambda: {"debit": D("0"), "credit": D("0")})
    for e in event.get("effects", []):
        cur = e["amount"]["currency"]
        totals[cur][e["entry_type"]] += D(e["amount"]["amount"])
    for cur, t in totals.items():
        if t["debit"] != t["credit"]:
            raise LedgerError(
                f"事件 {event['event_id']} 币种 {cur} 借贷不平：借 {t['debit']} ≠ 贷 {t['credit']}"
            )


def replay(events: Iterable[dict]) -> Ledger:
    """按 occurred_at 重放事件，登记科目、贡献者应付与批次封账状态。"""
    events = sorted(events, key=lambda e: e["occurred_at"])
    ledger = Ledger()

    for ev in events:
        etype = ev["event_type"]
        agg = ev["aggregate_type"]
        links = _links(ev)

        if etype == "SETTLEMENT_OPENED" and agg == "settlement_batch":
            ledger.batches.setdefault(ev["aggregate_id"], Batch(ev["aggregate_id"]))
        if etype == "SETTLEMENT_CLOSED":
            batch = ledger.batches.setdefault(ev["aggregate_id"], Batch(ev["aggregate_id"]))
            batch.closed_at = ev["occurred_at"]

        if not ev.get("effects"):
            continue

        _check_event_balance(ev)

        batch_ref = links.get("period_batch", "")
        batch_id = batch_ref.split("/", 1)[1] if "/" in batch_ref else batch_ref
        if batch_id and batch_id in ledger.batches:
            batch = ledger.batches[batch_id]
            if batch.closed_at is not None and ev["occurred_at"] >= batch.closed_at:
                if etype != "ADJUSTMENT_POSTED":
                    raise LedgerError(
                        f"事件 {ev['event_id']} 试图改写已封账批次 {batch_id}；"
                        "迟到单据必须以 ADJUSTMENT_POSTED 进入新批次"
                    )
            batch.effects.extend(ev["effects"])

        if etype == "ADJUSTMENT_POSTED":
            target = links.get("target_batch", "")
            target_id = target.split("/", 1)[1] if "/" in target else target
            target_batch = ledger.batches.get(target_id)
            if not target_id:
                raise LedgerError(f"调整事件 {ev['event_id']} 缺少 target_batch")
            # 目标批次在本次重放切片中时，必须已封账；切片外（跨批重放）不强行校验
            if target_batch is not None and target_batch.closed_at is None:
                raise LedgerError(f"调整事件 {ev['event_id']} 的 target_batch {target_id} 尚未封账")
            if batch_id and batch_id == target_id:
                raise LedgerError(f"调整事件 {ev['event_id']} 不得直接落回被调整的封账批次")

        for e in ev["effects"]:
            acct, cur = e["account"], e["amount"]["currency"]
            signed = D(e["amount"]["amount"]) * (1 if e["entry_type"] == "credit" else -1)
            ledger.balances[acct][cur] += signed
            if acct == "contributor_payable" and e.get("memo"):
                who = e["memo"].split()[0]
                ledger.payable[who][cur] += signed

        ledger.posted += 1

    return ledger


def check_conservation(events: Iterable[dict]) -> None:
    """整批重放并验证全币种试算平衡。"""
    ledger = replay(events)
    for cur, t in ledger.trial_balance().items():
        if t["debits"] != t["credits"]:
            raise LedgerError(f"币种 {cur} 试算不平衡：借 {t['debits']} ≠ 贷 {t['credits']}")


def check_deferred(events: Iterable[dict]) -> None:
    """预付会员收入只能随实际消费确认：不得超确认，退款只退未消费递延。"""
    by_payment: dict[str, dict[str, Decimal]] = defaultdict(lambda: {
        "received": D("0"), "recognized": D("0"), "refunded": D("0")
    })
    for ev in events:
        links = _links(ev)
        for e in ev.get("effects", []):
            acct, amt = e["account"], D(e["amount"]["amount"])
            if acct == "deferred_revenue":
                if ev["event_type"] == "USER_PAYMENT_RECEIVED":
                    pay = f"payment/{ev['aggregate_id']}"
                elif ev["event_type"] == "REFUND_RECORDED":
                    pay = links.get("original_payment", "")
                else:
                    pay = links.get("payment", "")
                if ev["event_type"] == "USER_PAYMENT_RECEIVED" and e["entry_type"] == "credit":
                    by_payment[pay]["received"] += amt
                elif ev["event_type"] == "DEFERRED_REVENUE_RECOGNIZED" and e["entry_type"] == "debit":
                    by_payment[pay]["recognized"] += amt
                elif ev["event_type"] == "REFUND_RECORDED" and e["entry_type"] == "debit":
                    by_payment[pay]["refunded"] += amt
    for pay, v in by_payment.items():
        if v["recognized"] > v["received"]:
            raise LedgerError(f"{pay} 递延确认 {v['recognized']} 超过实收 {v['received']}")
        if v["recognized"] + v["refunded"] > v["received"]:
            raise LedgerError(f"{pay} 确认 {v['recognized']} + 退款 {v['refunded']} 超过实收 {v['received']}")
