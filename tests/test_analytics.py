import unittest

from src.analytics import sanitize_for_analytics, trend_projection
from tests.test_contract import load_events


def collect_keys(node, found=None):
    if found is None:
        found = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            collect_keys(value, found)
    elif isinstance(node, list):
        for item in node:
            collect_keys(item, found)
    return found


class AnalyticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.events = load_events("scenario_cross_media.json")
        cls.projection = trend_projection(cls.events)

    def test_projection_strips_personal_trajectory(self) -> None:
        keys = collect_keys(self.projection)
        self.assertFalse(keys & {"user_id", "device_id", "account_id", "read_trace"})

    def test_projection_keeps_work_level_fields(self) -> None:
        consumption = next(
            e for e in self.projection if e["event_type"] == "CONSUMPTION_RECORDED"
        )
        self.assertIn("asset_version_id", consumption["payload"])

    def test_unpublished_unit_price_hidden(self) -> None:
        plans = {
            e["aggregate_id"]: e["payload"]
            for e in self.projection
            if e["event_type"] == "PRICE_PLAN_PUBLISHED"
        }
        self.assertNotIn("unit_price", plans["pp-ad-01"])
        self.assertEqual(plans["pp-ad-01"]["visibility"], "internal")
        self.assertEqual(plans["pp-sub-01"]["unit_price"], 300)

    def test_sanitize_does_not_mutate_source(self) -> None:
        original = next(e for e in self.events if "user_id" in e.get("payload", {}))
        sanitize_for_analytics(original)
        self.assertIn("user_id", original["payload"])


if __name__ == "__main__":
    unittest.main()
