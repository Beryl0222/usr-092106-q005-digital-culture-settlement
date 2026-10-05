"""只读投影：创作者收入追溯 与 趋势分析脱敏视图。

- creator_view：从一笔收入出发，向创作者呈现对应消费、作品版本与扣减原因。
- trend_view：趋势分析只能读取 public_aggregates 聚合，读不到个人阅读轨迹
  或未公开单价（docs/domain.md 不变量 7、8）。
投影不修改事件，也不暴露 finance_internal / contributor_private 字段。
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Iterable

from .ledger import D


def _ref(ref: str) -> tuple[str, str]:
    parts = ref.split("/", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("", ref)


def _index(events: Iterable[dict]) -> dict[tuple[str, str], dict]:
    # 同一聚合多版本时保留最早事件（登记事件先于争议/下架等后继事件）。
    idx: dict[tuple[str, str], dict] = {}
    for e in sorted(events, key=lambda x: x["occurred_at"]):
        idx.setdefault((e["aggregate_type"], e["aggregate_id"]), e)
    return idx


# ----------------------------- 创作者视角 ------------------------------------

def _contributor_share(event: dict, contributor: str) -> Decimal | None:
    for s in event.get("subjects", []):
        if s["kind"] == "contributor" and s["key"] == contributor:
            for e in event.get("effects", []):
                if e["account"] == "contributor_payable" and e.get("memo", "").startswith(contributor):
                    signed = D(e["amount"]["amount"]) * (1 if e["entry_type"] == "credit" else -1)
                    return signed
    return None


def _deductions(events: list[dict], idx: dict, contributor: str, chain: set[str]) -> list[dict]:
    """收集沿该笔收入关联链（原支付/原收入/版本）发生的单笔扣减（拒付/退款/暂扣）。"""
    out: list[dict] = []
    for ev in events:
        if ev["event_type"] not in (
            "CHARGEBACK_RECORDED", "REFUND_RECORDED", "WITHHOLDING_PLACED"
        ):
            continue
        refs = {l["ref"] for l in ev.get("links", [])}
        if not (refs & chain):
            continue
        amount = D("0")
        for e in ev.get("effects", []):
            if e["account"] == "contributor_payable" and e.get("memo", "").startswith(contributor):
                amount += D(e["amount"]["amount"]) * (1 if e["entry_type"] == "credit" else -1)
        if amount < 0:
            out.append({
                "reason_event": ev["event_type"],
                "summary": ev["summary"],
                "amount": str(-amount),
                "currency": ev.get("body", {}).get("currency", "CNY"),
            })
    return out


def creator_revenue_view(events: Iterable[dict], revenue_event_id: str, contributor: str) -> dict:
    """回答创作者：这笔钱对应哪次消费、哪个版本、为什么被扣。"""
    events = sorted(events, key=lambda e: e["occurred_at"])
    idx = _index(events)

    target = next(
        (e for e in events if e["event_type"] == "REVENUE_ALLOCATED" and e["aggregate_id"] == revenue_event_id),
        None,
    )
    if target is None:
        # 允许直接用 aggregate_id 查找
        target = idx.get(("revenue_event", revenue_event_id))
    if target is None:
        raise KeyError(f"找不到收入事件 {revenue_event_id}")

    links = {link["rel"]: link["ref"] for link in target.get("links", [])}
    share = _contributor_share(target, contributor)
    if share is None:
        raise LookupError(f"贡献者 {contributor} 不在 {revenue_event_id} 的分账主体中")

    # 扣减原因的关联链：与该笔收入的版本/支付/消费/原收入直接相关；
    # 税费按同批次归集，暂扣不得仅凭同批次就挂到其他收入上。
    chain = {f"revenue_event/{target['aggregate_id']}", *links.values()}
    chain.discard(links.get("period_batch", ""))
    batch_ref = links.get("period_batch")

    consumption = idx.get(("consumption", _ref(links.get("consumption", "//"))[1])) if "consumption" in links else None
    version = idx.get(("content_version", _ref(links.get("version", "//"))[1])) if "version" in links else None
    work = idx.get(("work", _ref(links.get("work", "//"))[1])) if "work" in links else None
    adaptation = idx.get(("adaptation_link", _ref(links.get("adaptation", "//"))[1])) if "adaptation" in links else None
    source_work = idx.get(("work", _ref(links.get("source_work", "//"))[1])) if "source_work" in links else None

    batch_taxes = []
    if batch_ref:
        for ev in events:
            if ev["event_type"] != "TAX_ASSESSED":
                continue
            refs = {l["ref"] for l in ev.get("links", [])}
            if batch_ref not in refs:
                continue
            for e in ev.get("effects", []):
                if e["account"] == "contributor_payable" and e.get("memo", "").startswith(contributor):
                    batch_taxes.append({
                        "reason_event": "TAX_ASSESSED",
                        "summary": ev["summary"],
                        "amount": str(D(e["amount"]["amount"])),
                        "currency": ev.get("body", {}).get("currency", "CNY"),
                    })

    return {
        "revenue_event": target["aggregate_id"],
        "contributor": contributor,
        "gross": target["body"].get("gross"),
        "net_to_contributor": str(share),
        "currency": target["body"].get("currency", "CNY"),
        "consumption": {
            "id": consumption["aggregate_id"],
            "at": consumption["body"]["consumed_at"],
            "evidence_ref": consumption["body"].get("evidence_ref"),
        } if consumption else None,
        "version": {"id": version["aggregate_id"], "kind": version["body"]["kind"]} if version else None,
        "work": work["body"]["title"] if work else (source_work["body"]["title"] if source_work else None),
        "adaptation": adaptation["body"]["adaptation_type"] if adaptation else None,
        "contract_ref": adaptation["body"].get("contract_ref") if adaptation else None,
        "deductions": _deductions(events, idx, contributor, chain),
        "batch_level_deductions": batch_taxes,
    }


# ----------------------------- 趋势脱敏视角 ----------------------------------

TREND_SAFE_TYPES = {"REVENUE_ALLOCATED", "ADJUSTMENT_POSTED"}
_FORBIDDEN_ACCOUNTS = {"deferred_revenue"}  # 仅示意：与个人支付相关的科目不进趋势明细


class PrivacyViolation(Exception):
    """趋势投影试图读取个人轨迹或未公开单价。"""


def trend_view(events: Iterable[dict]) -> dict:
    """只聚合 visibility=public_aggregates 的收入事件。

    输出按媒介/作品维度的合计；绝不包含：
    - payment / entitlement / consumption 明细与任何消费者标识、evidence_ref；
    - confidential=true 的价格方案与未公开单价。
    """
    events = list(events)
    idx = _index(events)

    public = [
        e for e in events
        if e["event_type"] in TREND_SAFE_TYPES and e.get("visibility") == "public_aggregates"
    ]

    # 防御性断言：公共事件即使引用了支付或机密方案用于归属，
    # 投影也绝不解析、输出这些明细（只取毛额聚合）。
    for ev in public:
        if "internal_price" in ev.get("body", {}):
            raise PrivacyViolation(f"{ev['event_id']} 在公共聚合中暴露了未公开单价")

    by_work: dict[str, Decimal] = defaultdict(lambda: D("0"))
    by_kind: dict[str, Decimal] = defaultdict(lambda: D("0"))
    for ev in public:
        links = {l["rel"]: _ref(l["ref"])[1] for l in ev.get("links", [])}
        version = idx.get(("content_version", links.get("version", "")))
        work_id = None
        if version is not None:
            work_id = next((_ref(l["ref"])[1] for l in version.get("links", []) if l["rel"] == "of_work"), None)
        kind = version["body"]["kind"] if version is not None else "unknown"
        # 仅统计贡献者+平台分账基数（毛额），不输出单价字段
        gross = D(ev["body"].get("gross") or ev["body"].get("delta") or "0")
        if work_id:
            by_work[work_id] += gross
        by_kind[kind] += gross

    return {
        "granularity": "aggregate_only",
        "revenue_by_work": {k: str(v) for k, v in sorted(by_work.items())},
        "revenue_by_version_kind": {k: str(v) for k, v in sorted(by_kind.items())},
        "contains_individual_tracks": False,
        "contains_confidential_unit_prices": False,
    }


# ----------------------------- 复用与争议规则 --------------------------------

def excluded_reuse_parties(events: Iterable[dict]) -> dict[str, list[str]]:
    """对每个派生版本，给出因素材复用而不得重复计酬的主体（由原始贡献者推导）。"""
    events = list(events)
    idx = _index(events)
    result: dict[str, list[str]] = {}
    for ev in events:
        if ev["event_type"] != "MATERIAL_REUSE_RECORDED":
            continue
        links = {l["rel"]: _ref(l["ref"])[1] for l in ev.get("links", [])}
        source = idx.get(("content_version", links["reuse_of"]))
        parties: list[str] = []
        if source is not None:
            source_work = next((_ref(l["ref"])[1] for l in source.get("links", []) if l["rel"] == "of_work"), None)
            parties = sorted({
                c["body"]["contributor_id"]
                for c in events
                if c["event_type"] == "CONTRIBUTOR_ADDED"
                and any(l["rel"] == "work" and _ref(l["ref"])[1] == source_work for l in c.get("links", []))
            })
        result[links["derived_version"]] = parties
    return result


def uncontested_payouts_unaffected(events: Iterable[dict]) -> None:
    """争议暂扣不得拖住无争议贡献者：被暂扣主体必须出现在某个争议 scope 中。"""
    events = list(events)
    disputed: set[str] = set()
    for ev in events:
        if ev["event_type"] == "DISPUTE_OPENED":
            who = ev["body"].get("scope", {}).get("contributor")
            if who:
                disputed.add(who)
    for ev in events:
        if ev["event_type"] != "WITHHOLDING_PLACED":
            continue
        held = {s["key"] for s in ev.get("subjects", []) if s["kind"] == "contributor"}
        outside = held - disputed
        if outside:
            raise PrivacyViolation(f"暂扣事件 {ev['event_id']} 波及无争议贡献者 {outside}")
