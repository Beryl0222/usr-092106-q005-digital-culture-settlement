"""地域与期限权利的范围判定。

合同修订、作者退出、侵权冻结、内容下架都只约束其*显式 scope* 命中的
权利范围（docs/domain.md 不变量 5）。本模块把权利事件重放成一张可查询
的注册表，但不改变任何事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


def _ref(ref: str) -> tuple[str, str]:
    """'content_version/V-EP-01' -> ('content_version', 'V-EP-01')。"""
    parts = ref.split("/", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("", ref)


def _date(ts: str) -> str:
    return ts[:10]


def _scope_hits(scope: dict | None, *, media: str | None = None, territory: str | None = None,
                use: str | None = None, version: str | None = None) -> bool:
    """无 scope 或 scope 中未设限的维度视为不约束（最小约束原则）。"""
    if not scope:
        return False
    if "media" in scope and media is not None and media not in scope["media"]:
        return False
    if "territories" in scope and territory is not None and territory not in scope["territories"]:
        return False
    if "uses" in scope and use is not None and use not in scope["uses"]:
        return False
    if "version" in scope and version is not None and scope["version"] != version:
        return False
    return True


@dataclass
class GrantState:
    grant_id: str
    body: dict
    # 版本级冻结：version -> scope
    freezes: list[dict] = field(default_factory=list)
    amendments: list[dict] = field(default_factory=list)
    withdrawal: dict | None = None

    def active(self, at: str, *, media: str, territory: str = "CN",
               use: str | None = None, version: str | None = None) -> bool:
        if not (self.body["valid_from"] <= _date(at) <= self.body["valid_to"]):
            return False
        if media not in self.body.get("media", []):
            return False
        if territory not in self.body.get("territories", []):
            return False
        if use is not None and use not in self.body.get("uses", []):
            return False
        # 作者退出：只约束未来使用（future_uses_only），且按其 scope 的媒介/期限
        if self.withdrawal and _date(at) >= self.withdrawal.get("valid_from", "9999"):
            ws = self.withdrawal.get("scope", {})
            if ws.get("future_uses_only", False) and _scope_hits(ws, media=media, territory=territory, use=use):
                return False
        # 侵权冻结：只冻结命中版本/范围的权利，其余版本照常
        for fz in self.freezes:
            if _scope_hits(fz.get("scope"), media=media, territory=territory, use=use, version=version):
                return False
        return True

    def term(self, dotted_key: str, at: str) -> str:
        """读取合同条款，套用在 at 当日（含）已生效且不追溯的修订。"""
        value = self.body
        for part in dotted_key.split("."):
            value = value[part]
        for am in sorted(self.amendments, key=lambda e: e["occurred_at"]):
            eff = am["body"].get("effective_from", "9999")
            if not am["body"].get("retroactive", False) and _date(at) < eff:
                continue
            if dotted_key in am["body"].get("changes", {}):
                value = am["body"]["changes"][dotted_key]
        return value


class RightsRegistry:
    def __init__(self, events: Iterable[dict]):
        self.grants: dict[str, GrantState] = {}
        self.adaptations: dict[str, dict] = {}
        self.takedowns: dict[str, list[dict]] = {}
        self.version_work: dict[str, str] = {}
        for ev in sorted(events, key=lambda e: e["occurred_at"]):
            self._apply(ev)

    def _apply(self, ev: dict) -> None:
        t, aid = ev["event_type"], ev["aggregate_id"]
        if t == "RIGHTS_GRANTED":
            self.grants[aid] = GrantState(aid, ev["body"])
        elif t == "GRANT_AMENDED":
            self.grants[aid].amendments.append(ev)
        elif t == "GRANT_FROZEN":
            self.grants[aid].freezes.append(ev["body"])
        elif t == "GRANT_WITHDRAWN":
            self.grants[aid].withdrawal = ev["body"]
        elif t == "ADAPTATION_LINKED":
            self.adaptations[aid] = ev
        elif t == "CONTENT_TAKEN_DOWN":
            self.takedowns.setdefault(aid, []).append(ev["body"])
        elif t == "VERSION_REGISTERED":
            for link in ev.get("links", []):
                if link["rel"] == "of_work":
                    self.version_work[aid] = _ref(link["ref"])[1]

    # ---- 查询 ----------------------------------------------------------------

    def contract_for(self, adaptation_id: str) -> str:
        """一次跨媒介改编按哪份合同分账：取改编关系上挂的授权。"""
        for link in self.adaptations[adaptation_id].get("links", []):
            if link["rel"] == "grant":
                return _ref(link["ref"])[1]
        raise KeyError(f"改编关系 {adaptation_id} 未挂载授权合同")

    def drama_share_for(self, adaptation_id: str, at: str) -> str:
        return self.grants[self.contract_for(adaptation_id)].term("revenue_terms.drama_share", at)

    def consumption_allowed(self, version: str, at: str, territory: str = "CN") -> bool:
        """下架只阻止其 scope 内的*新增*消费。"""
        for td in self.takedowns.get(version, []):
            scope = td.get("scope", {})
            if territory in scope.get("territories", [territory]):
                return False
        return True
