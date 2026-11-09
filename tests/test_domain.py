import unittest

from mvp_task.domain import normalize, quality


class QualityRulesTest(unittest.TestCase):
    def test_suspicious_fact_needs_explicit_review(self):
        row = normalize("preparation", {
            "record_id": "P-003", "date": "07.10.2026", "batch_id": "LOT-03",
            "employee": "Иван Морозов", "plan_qty": "100", "fact_qty": "1200",
        })
        self.assertEqual(("pending", False), (quality("preparation", row)[0], quality("preparation", row)[2]))
        row["fact_qty"] = 120
        self.assertEqual("approved", quality("preparation", row)[0])

    def test_missing_and_negative_cannot_be_accepted_as_fact(self):
        row = normalize("preparation", {
            "record_id": "P-004", "date": "2026-10-08", "batch_id": "LOT-04",
            "employee": "Иван Морозов", "plan_qty": 100, "fact_qty": -5,
        })
        self.assertTrue(quality("preparation", row)[2])
        row["fact_qty"] = None
        self.assertTrue(quality("preparation", row)[2])
        row["fact_qty"] = 0
        self.assertEqual("approved", quality("preparation", row)[0])


if __name__ == "__main__":
    unittest.main()
