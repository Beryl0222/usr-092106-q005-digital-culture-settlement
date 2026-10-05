import json
import unittest
from pathlib import Path

from src.validator import validate_event

ROOT = Path(__file__).parents[1]


def load_events(name: str) -> list[dict]:
    return json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))["events"]


class ContractTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(sample), [])


class ScenarioContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(
            (ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8")
        )
        cls.events = load_events("scenario_cross_media.json")

    def test_events_match_envelope(self) -> None:
        for event in self.events:
            self.assertEqual(validate_event(event), [], event["event_id"])

    def test_events_use_declared_vocabulary(self) -> None:
        event_types = set(self.schema["properties"]["event_type"]["enum"])
        aggregate_types = set(self.schema["properties"]["aggregate_type"]["enum"])
        for event in self.events:
            self.assertIn(event["event_type"], event_types, event["event_id"])
            self.assertIn(event["aggregate_type"], aggregate_types, event["event_id"])

    def test_event_ids_are_unique(self) -> None:
        ids = [event["event_id"] for event in self.events]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
