import copy
import unittest

from src.ledger import conservation_gap, replay_batch
from tests.test_contract import load_events


class LedgerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.events = load_events("scenario_cross_media.json")

    def test_august_batch_conserves(self) -> None:
        books = replay_batch(self.events, "batch-2026-08")
        self.assertEqual(conservation_gap(books), {"CNY": 0})
        self.assertEqual(books["CNY"]["recognized"], 2600)
        self.assertEqual(books["CNY"]["allocated"], 2140)
        self.assertEqual(books["CNY"]["deducted"], 460)

    def test_september_batch_conserves_with_adjustment_and_chargeback(self) -> None:
        books = replay_batch(self.events, "batch-2026-09")
        self.assertEqual(conservation_gap(books), {"CNY": 0})
        self.assertEqual(books["CNY"]["recognized"], 1000)
        self.assertEqual(books["CNY"]["adjustments"], 150)
        self.assertEqual(books["CNY"]["reversals"], 500)
        self.assertEqual(books["CNY"]["escrow_held"], 46)

    def test_october_batch_conserves_with_escrow_release(self) -> None:
        books = replay_batch(self.events, "batch-2026-10")
        self.assertEqual(conservation_gap(books), {"CNY": 0})
        self.assertEqual(books["CNY"]["escrow_released"], 46)
        self.assertEqual(books["CNY"]["allocated"], 46)

    def test_tampered_entry_breaks_conservation(self) -> None:
        forged = copy.deepcopy(self.events)
        for event in forged:
            if event["aggregate_id"] == "alloc-08-studio":
                event["payload"]["amount"] += 1
        gap = conservation_gap(replay_batch(forged, "batch-2026-08"))
        self.assertEqual(gap["CNY"], -1)

    def test_undisputed_contributors_settle_despite_escrow(self) -> None:
        books = replay_batch(self.events, "batch-2026-09")
        allocated = books["CNY"]["allocated"]
        # 争议只暂存白驹文化的 46 分，阿岚与其他分账在同一批次正常入账
        self.assertEqual(allocated, 554)
        self.assertEqual(books["CNY"]["escrow_held"], 46)


if __name__ == "__main__":
    unittest.main()
