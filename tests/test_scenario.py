import json
import unittest
from decimal import Decimal
from pathlib import Path

from src.access import _scope_hits
from src.validator import validate_event
from src.ledger import D, LedgerError, check_conservation, check_deferred, replay
from src.access import RightsRegistry
from src.projection import (
    creator_revenue_view,
    excluded_reuse_parties,
    trend_view,
    uncontested_payouts_unaffected,
)

ROOT = Path(__file__).parents[1]


def load(name: str):
    return json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))


def by_id(events):
    return {e["event_id"]: e for e in events}


def batch_events(events, batch_id):
    prefix = f"settlement_batch/{batch_id}"
    return [
        e for e in events
        if any(l["rel"] == "period_batch" and l["ref"] == prefix for l in e.get("links", []))
    ]


class ScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events = load("scenario.json")["events"]
        cls.ids = by_id(cls.events)

    # ---------- 0. 信封与 schema ------------------------------------------

    def test_all_events_match_envelope(self):
        for ev in self.events:
            self.assertEqual(validate_event(ev), [], ev["event_id"])

    def test_json_schema_valid(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("未安装 jsonschema")
        schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        jsonschema.validate(load("sample.json"), schema)
        for ev in self.events:
            jsonschema.validate(ev, schema)

    # ---------- 不变量 1：守恒 ---------------------------------------------

    def test_each_balanced_event(self):
        for ev in self.events:
            totals = {}
            for e in ev.get("effects", []):
                cur = e["amount"]["currency"]
                d, c = totals.setdefault(cur, [Decimal(0), Decimal(0)])
                if e["entry_type"] == "debit":
                    d += Decimal(e["amount"]["amount"])
                else:
                    c += Decimal(e["amount"]["amount"])
            for cur, (d, c) in totals.items():
                self.assertEqual(d, c, f"{ev['event_id']} {cur} 借贷不平")

    def test_global_trial_balance(self):
        check_conservation(self.events)
        tb = replay(self.events).trial_balance()
        self.assertEqual(tb["CNY"]["debits"], tb["CNY"]["credits"])

    def test_september_batch_balance_and_residual(self):
        ledger = replay(batch_events(self.events, "BATCH-SEP"))
        tb = ledger.trial_balance()
        self.assertEqual(tb["CNY"]["debits"], tb["CNY"]["credits"])
        # 封账期末：在途资金 14.20 = 平台留存 9.70 + 税 2.00 + 暂扣 2.50
        self.assertEqual(ledger.balances["cash_clearing"]["CNY"], D("-14.20"))
        self.assertEqual(ledger.balances["platform_retained"]["CNY"], D("9.70"))
        self.assertEqual(ledger.balances["tax_payable"]["CNY"], D("2.00"))
        self.assertEqual(ledger.balances["withholding_escrow"]["CNY"], D("2.50"))

    def test_all_contributors_settled_to_zero(self):
        ledger = replay(self.events)
        for who, amt in ledger.payable.items():
            self.assertEqual(amt["CNY"], D("0"), f"{who} 应付未结平：{amt['CNY']}")

    # ---------- 不变量 2：封账不可变，迟到单只能差额调整 -------------------

    def test_closed_batch_rejects_direct_post(self):
        tampered = {
            "event_id": "EV-FORGE",
            "event_type": "REVENUE_ALLOCATED",
            "aggregate_type": "revenue_event",
            "aggregate_id": "R-FORGE",
            "occurred_at": "2026-10-06T10:00:00+08:00",
            "version": 1,
            "summary": "试图在已封账的9月批次补记收入",
            "links": [{"rel": "period_batch", "ref": "settlement_batch/BATCH-SEP"}],
            "effects": [
                {"account": "cash_clearing", "entry_type": "debit", "amount": {"amount": "1.00", "currency": "CNY"}},
                {"account": "revenue:ad_share", "entry_type": "credit", "amount": {"amount": "1.00", "currency": "CNY"}},
            ],
        }
        with self.assertRaises(LedgerError):
            replay([*self.events, tampered])

    def test_late_statement_enters_via_adjustment_in_new_batch(self):
        adj = self.ids["EV-ADJ-LATE-AD"]
        refs = {l["rel"]: l["ref"] for l in adj["links"]}
        self.assertEqual(refs["target_batch"], "settlement_batch/BATCH-SEP")
        self.assertEqual(refs["period_batch"], "settlement_batch/BATCH-OCT")
        self.assertEqual(adj["event_type"], "ADJUSTMENT_POSTED")
        # 9月批次自身分录不含迟到广告（冻结），10月批次含调整且各自平衡
        sep = replay(batch_events(self.events, "BATCH-SEP"))
        self.assertNotIn("R-ADJ-LATE-AD", [e["aggregate_id"] for e in batch_events(self.events, "BATCH-SEP")])
        self.assertEqual(sep.balances["revenue:ad_share"].get("CNY", D("0")), D("0"))
        oct_ledger = replay(batch_events(self.events, "BATCH-OCT"))
        self.assertEqual(oct_ledger.trial_balance()["CNY"]["debits"],
                         oct_ledger.trial_balance()["CNY"]["credits"])
        # 整账重放不因调整而报错
        check_conservation(self.events)

    # ---------- 不变量 3：预付随实际消费确认，退款只退未消费 ---------------

    def test_deferred_recognized_by_actual_consumption(self):
        check_deferred(self.events)
        ev = self.ids["EV-DEFER-SEP"]
        self.assertEqual(ev["body"]["recognition_basis"], "actual_consumption")
        self.assertEqual(ev["body"]["earned"], "40.00")
        self.assertEqual(ev["body"]["remaining_deferred"], "28.00")
        refund = self.ids["EV-REFUND-BUNDLE"]["body"]
        self.assertEqual(refund["refund_amount"], "28.00")
        self.assertEqual(refund["recognized_not_refunded"], "40.00")

    # ---------- 不变量 4：素材复用不重复计酬 -------------------------------

    def test_reuse_excludes_original_contributors(self):
        excluded = excluded_reuse_parties(self.events)
        # 回放整场复用直播：主播/传承人不得就回放再分一次
        self.assertEqual(set(excluded["V-REPLAY-22"]), {"C-B1", "C-C1"})
        # 剪辑复用直播片段：同样排除原素材贡献者
        self.assertEqual(set(excluded["V-CLIP-08"]), {"C-B1", "C-C1"})
        adj = self.ids["EV-ADJ-LATE-AD"]
        payees = {s["key"] for s in adj["subjects"] if s["kind"] == "contributor"}
        self.assertEqual(payees, {"C-D1"})  # 只有剪辑师就新增价值计酬
        memo_parties = {e["memo"] for e in adj["effects"] if e["account"] == "contributor_payable"}
        self.assertEqual(memo_parties, {"C-D1 剪辑新增价值"})

    # ---------- 不变量 5：修订/退出/冻结/下架只约束明确范围 -----------------

    def test_contract_amendment_is_scoped_and_non_retroactive(self):
        reg = RightsRegistry(self.events)
        adaptation = "AD-NOVEL-DRAMA-EP01"
        self.assertEqual(reg.contract_for(adaptation), "G-1")
        self.assertEqual(reg.drama_share_for(adaptation, "2026-09-30T23:06:00+08:00"), "0.25")
        self.assertEqual(reg.drama_share_for(adaptation, "2026-10-01T00:00:00+08:00"), "0.28")

    def test_freeze_only_hits_named_scope(self):
        reg = RightsRegistry(self.events)
        # 第1集：授权有效，正常分账
        self.assertTrue(reg.grants["G-1"].active(
            "2026-09-11T20:00:00+08:00", media="short_drama", territory="CN",
            use="adaptation", version="V-EP-01"))
        # 第3集：被冻结
        self.assertFalse(reg.grants["G-1"].active(
            "2026-09-29T10:00:00+08:00", media="short_drama", territory="CN",
            use="adaptation", version="V-EP-03"))
        # 冻结不波及其他地域：冻结 scope 本身不命中 CN 以外
        scope = reg.grants["G-1"].freezes[0]["scope"]
        self.assertFalse(_scope_hits(scope, media="short_drama", territory="US",
                                     use="adaptation", version="V-EP-03"))
        self.assertTrue(_scope_hits(scope, media="short_drama", territory="CN",
                                    use="adaptation", version="V-EP-03"))

    def test_takedown_blocks_only_new_in_scope_consumption(self):
        reg = RightsRegistry(self.events)
        self.assertFalse(reg.consumption_allowed("V-EP-03", "2026-09-30T00:00:00+08:00"))
        self.assertTrue(reg.consumption_allowed("V-EP-01", "2026-09-30T00:00:00+08:00"))
        # 下架发生在9月29日，9月12日的历史消费仍然存在且已分账
        self.assertIn("EV-RA-EP03", self.ids)

    def test_withdrawal_is_future_only(self):
        reg = RightsRegistry(self.events)
        # 9月15日直播授权有效，历史打照常分
        self.assertTrue(reg.grants["G-2"].active(
            "2026-09-15T20:30:00+08:00", media="live_heritage", territory="CN", use="stream"))
        # 10月起未来直播场次授权终止
        self.assertFalse(reg.grants["G-2"].active(
            "2026-10-10T20:00:00+08:00", media="live_heritage", territory="CN", use="stream"))
        # 退出范围只含 live_heritage，剪辑片段授权不受影响
        self.assertTrue(reg.grants["G-2"].active(
            "2026-10-10T20:00:00+08:00", media="clip", territory="CN", use="clip_snippet"))

    # ---------- 不变量 6：争议暂扣不拖无争议贡献者 -------------------------

    def test_withholding_isolated_to_disputed_party(self):
        uncontested_payouts_unaffected(self.events)
        wh = self.ids["EV-WITHHOLD-EP03-A1"]
        self.assertEqual({s["key"] for s in wh["subjects"]}, {"C-A1"})
        # 制作方在第3集的6元与其余收入照常进入9月排款（12.00+2.40+6.00=20.40）
        scheduled = self.ids["EV-PAYOUT-SEP"]["body"]["scheduled"]
        self.assertEqual(scheduled["C-M1"], "20.40")
        self.assertNotIn("withhold", scheduled)

    def test_chargeback_reverses_proportionally(self):
        # 拒付2元后，林晚当期：6 - 1.2(拒付) - 1(税) = 3.8 照常排款
        scheduled = self.ids["EV-PAYOUT-SEP"]["body"]["scheduled"]
        self.assertEqual(scheduled["C-B1"], "3.80")
        self.assertEqual(scheduled["C-C1"], "1.60")

    # ---------- 不变量 7：单笔收入可追溯 -----------------------------------

    def test_creator_can_trace_revenue_to_consumption_version_deductions(self):
        view = creator_revenue_view(self.events, "R-RA-EP01-SEP", "C-A1")
        self.assertEqual(view["gross"], "20.00")
        self.assertEqual(view["net_to_contributor"], "5.00")
        self.assertEqual(view["consumption"]["id"], "X-EP-5002")
        self.assertEqual(view["version"], {"id": "V-EP-01", "kind": "drama_episode"})
        self.assertEqual(view["work"], "云岭匠心记")
        self.assertEqual(view["adaptation"], "novel_to_short_drama")
        self.assertEqual(view["contract_ref"], "HT-A1-2026-01")
        reasons = {d["reason_event"] for d in view["deductions"]}
        # 第1集无争议，不应出现第3集的暂扣
        self.assertNotIn("WITHHOLDING_PLACED", reasons)
        self.assertEqual(reasons, set())
        # 批次级代扣个税单独呈现，不重复挂到每笔收入上
        batch_tax = {d["reason_event"]: d["amount"] for d in view["batch_level_deductions"]}
        self.assertEqual(batch_tax, {"TAX_ASSESSED": "1.00"})

    def test_creator_sees_chargeback_on_tip(self):
        view = creator_revenue_view(self.events, "R-RA-TIP-SEP", "C-B1")
        self.assertEqual(view["consumption"]["id"], "X-LIVE-5004")
        deductions = {d["reason_event"]: d["amount"] for d in view["deductions"]}
        self.assertEqual(deductions["CHARGEBACK_RECORDED"], "1.20")
        batch_tax = {d["reason_event"]: d["amount"] for d in view["batch_level_deductions"]}
        self.assertEqual(batch_tax["TAX_ASSESSED"], "1.00")

    def test_creator_sees_withholding_reason(self):
        view = creator_revenue_view(self.events, "R-RA-EP03-SEP", "C-A1")
        deductions = {d["reason_event"]: d["amount"] for d in view["deductions"]}
        self.assertEqual(deductions["WITHHOLDING_PLACED"], "2.50")

    # ---------- 不变量 8：趋势视图无个人轨迹/未公开单价 --------------------

    def test_trend_view_is_aggregate_only(self):
        tv = trend_view(self.events)
        self.assertFalse(tv["contains_individual_tracks"])
        self.assertFalse(tv["contains_confidential_unit_prices"])
        flat = json.dumps(tv, ensure_ascii=False)
        for leak in ("P-BUNDLE", "P-TIP", "P-UNLOCK", "E-MEM", "X-EP-", "X-CH-", "X-LIVE",
                     "play-token", "internal_price", "68.00"):
            self.assertNotIn(leak, flat, f"趋势视图泄漏：{leak}")
        # 按媒介聚合可统计：网文10、短剧订阅+解锁共34、打赏10、迟到广告归剪辑16
        self.assertEqual(tv["revenue_by_version_kind"]["chapter"], "10.00")
        self.assertEqual(tv["revenue_by_version_kind"]["drama_episode"], "34.00")
        self.assertEqual(tv["revenue_by_version_kind"]["live_session"], "10.00")
        self.assertEqual(tv["revenue_by_version_kind"]["clip"], "16.00")
        # 回放纯复用、无新增计酬，不产生趋势收入
        self.assertNotIn("replay", tv["revenue_by_version_kind"])


if __name__ == "__main__":
    unittest.main()
