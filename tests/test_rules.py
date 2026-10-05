import unittest

from src.rules import (
    check_closed_period,
    check_deferred_cap,
    check_escrow_scope,
    check_no_double_pay,
    check_rights_constraint,
)
from tests.test_contract import load_events


def make_event(event_id, event_type, aggregate_type="revenue_event", occurred_at="2026-09-01T00:00:00+08:00", **payload):
    return {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": payload.pop("aggregate_id", event_id),
        "occurred_at": occurred_at,
        "version": 1,
        "summary": event_id,
        "payload": payload,
    }


def open_batch(batch_id, at="2026-08-01T09:00:00+08:00"):
    return make_event(f"open-{batch_id}", "SETTLEMENT_BATCH_OPENED", "settlement_batch",
                      occurred_at=at, aggregate_id=batch_id,
                      period_start="2026-08-01", period_end="2026-08-31")


def close_batch(batch_id, at="2026-09-05T10:00:00+08:00"):
    return make_event(f"close-{batch_id}", "SETTLEMENT_CLOSED", "settlement_batch",
                      occurred_at=at, aggregate_id=batch_id, period_end="2026-08-31")


class ScenarioRuleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.events = load_events("scenario_cross_media.json")

    def test_scenario_passes_all_rules(self) -> None:
        for check in (
            check_closed_period,
            check_no_double_pay,
            check_deferred_cap,
            check_escrow_scope,
            check_rights_constraint,
        ):
            self.assertEqual(check(self.events), [], check.__name__)


class ClosedPeriodRuleTest(unittest.TestCase):
    def test_direct_entry_into_closed_batch_rejected(self) -> None:
        events = [
            open_batch("b1"),
            close_batch("b1"),
            make_event("x1", "REVENUE_ALLOCATED", occurred_at="2026-09-06T00:00:00+08:00",
                       amount=100, settlement_batch_id="b1"),
        ]
        self.assertEqual(len(check_closed_period(events)), 1)

    def test_adjustment_must_target_closed_batch(self) -> None:
        events = [
            open_batch("b1"),
            open_batch("b2"),
            make_event("x1", "LATE_STATEMENT_ADJUSTED", "channel_statement",
                       occurred_at="2026-09-06T00:00:00+08:00",
                       amount=50, adjusts_batch_id="b1", settlement_batch_id="b2"),
        ]
        self.assertEqual(check_closed_period(events), ["x1：差额调整必须指向已封账批次"])

    def test_adjustment_into_closed_batch_rejected(self) -> None:
        events = [
            open_batch("b1"),
            close_batch("b1"),
            make_event("x1", "LATE_STATEMENT_ADJUSTED", "channel_statement",
                       occurred_at="2026-09-06T00:00:00+08:00",
                       amount=50, adjusts_batch_id="b1", settlement_batch_id="b1"),
        ]
        errors = check_closed_period(events)
        self.assertTrue(any("不得入账到已封账批次" in e for e in errors))

    def test_unopened_batch_rejected(self) -> None:
        events = [
            make_event("x1", "REVENUE_RECOGNIZED", amount=100, settlement_batch_id="b9"),
        ]
        self.assertTrue(any("未开启" in e for e in check_closed_period(events)))


class NoDoublePayRuleTest(unittest.TestCase):
    def test_double_recognition_rejected(self) -> None:
        events = [
            make_event("c1", "CONSUMPTION_RECORDED", user_id="u1"),
            make_event("r1", "REVENUE_RECOGNIZED", consumption_id="c1", amount=100),
            make_event("r2", "REVENUE_RECOGNIZED", consumption_id="c1", amount=100),
        ]
        self.assertEqual(len(check_no_double_pay(events)), 1)

    def test_reused_material_not_remunerated(self) -> None:
        events = [
            make_event("c1", "CONSUMPTION_RECORDED", user_id="u1", reuse_of="ver-x"),
            make_event("r1", "REVENUE_RECOGNIZED", consumption_id="c1", amount=100),
        ]
        errors = check_no_double_pay(events)
        self.assertTrue(any("不得重复计酬" in e for e in errors))


class DeferredCapRuleTest(unittest.TestCase):
    def test_overdraw_rejected(self) -> None:
        events = [
            make_event("d1", "REVENUE_DEFERRED", payment_id="p1", amount=300),
            make_event("r1", "REVENUE_RECOGNIZED", deferred_from="p1", amount=200),
            make_event("r2", "REVENUE_RECOGNIZED", deferred_from="p1", amount=200),
        ]
        self.assertEqual(len(check_deferred_cap(events)), 1)

    def test_recognition_plus_refund_capped(self) -> None:
        events = [
            make_event("d1", "REVENUE_DEFERRED", payment_id="p1", amount=300),
            make_event("r1", "REVENUE_RECOGNIZED", deferred_from="p1", amount=200),
            make_event("f1", "REFUND_RECORDED", "payment", deferred_from="p1", amount=200),
        ]
        self.assertEqual(len(check_deferred_cap(events)), 1)


class EscrowScopeRuleTest(unittest.TestCase):
    def test_non_party_escrow_rejected(self) -> None:
        events = [
            make_event("dp1", "DISPUTE_OPENED", "dispute_case", parties=["a", "b"]),
            make_event("e1", "ESCROW_HELD", "dispute_case",
                       dispute_id="dp1", contributor_id="c", amount=10),
        ]
        errors = check_escrow_scope(events)
        self.assertTrue(any("非当事方" in e for e in errors))

    def test_escrow_after_resolution_rejected(self) -> None:
        events = [
            make_event("dp1", "DISPUTE_OPENED", "dispute_case", parties=["a", "b"]),
            make_event("dp1r", "DISPUTE_RESOLVED", "dispute_case", aggregate_id="dp1"),
            make_event("e1", "ESCROW_HELD", "dispute_case",
                       dispute_id="dp1", contributor_id="a", amount=10),
        ]
        errors = check_escrow_scope(events)
        self.assertTrue(any("已结案" in e for e in errors))


class RightsConstraintRuleTest(unittest.TestCase):
    def test_allocation_after_constraint_rejected(self) -> None:
        events = [
            make_event("rc1", "RIGHTS_SCOPE_CONSTRAINED", "rights_term",
                       occurred_at="2026-09-01T00:00:00+08:00",
                       reason="侵权冻结", rights_term_ids=["rt-1"],
                       effective_from="2026-09-02T00:00:00+08:00"),
            make_event("a1", "REVENUE_ALLOCATED",
                       occurred_at="2026-09-03T00:00:00+08:00",
                       rights_term_id="rt-1", amount=100),
        ]
        self.assertEqual(len(check_rights_constraint(events)), 1)

    def test_unrelated_rights_term_unaffected(self) -> None:
        events = [
            make_event("rc1", "RIGHTS_SCOPE_CONSTRAINED", "rights_term",
                       occurred_at="2026-09-01T00:00:00+08:00",
                       reason="内容下架", rights_term_ids=["rt-1"],
                       effective_from="2026-09-02T00:00:00+08:00"),
            make_event("a1", "REVENUE_ALLOCATED",
                       occurred_at="2026-09-03T00:00:00+08:00",
                       rights_term_id="rt-2", amount=100),
        ]
        self.assertEqual(check_rights_constraint(events), [])


if __name__ == "__main__":
    unittest.main()
