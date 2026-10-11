"""The separate revision-marking pass. Invented references only."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

from evidence import Evidence  # noqa: E402
from marking_pass import is_marking_gap, marking_turns, portal_answer  # noqa: E402
from resolver import resolve  # noqa: E402
from test_resolver import base  # noqa: E402


def run(data):
    return resolve(Evidence.model_validate(data))


class MarkingPass(unittest.TestCase):
    def test_unmarked_revisions_are_the_marking_gap(self):
        data = base()
        for revision in data["revisions"]:
            revision["marking"] = "unknown"
        resolution = run(data)
        self.assertIsNone(resolution.answer)
        self.assertTrue(any(is_marking_gap(gap) for gap in resolution.gaps))

    def test_missing_marking_is_not_submitted_as_no_bid(self):
        data = base()
        for revision in data["revisions"]:
            revision["marking"] = "unknown"
        resolution = run(data)
        self.assertIsNone(portal_answer(resolution.answer, resolution.gaps, "AB-1000"))

    def test_a_priced_answer_is_kept(self):
        resolution = run(base())
        answer = portal_answer(resolution.answer, resolution.gaps, "AB-1000")
        self.assertEqual(answer["decision"], "bid")

    def test_unpriced_case_is_not_submitted_as_no_bid(self):
        data = base()
        data["positions"] = []
        resolution = run(data)
        self.assertFalse(any(is_marking_gap(gap) for gap in resolution.gaps))
        self.assertIsNone(portal_answer(resolution.answer, resolution.gaps, "AB-1000"))

    def test_true_no_bid_from_resolver_is_kept(self):
        data = base()
        data["materials"] = []
        data["erp_searches"] = [{"catalog_group": "G-1", "material_numbers": [], "complete": True}]
        resolution = run(data)
        answer = portal_answer(resolution.answer)
        self.assertIsNotNone(answer)
        self.assertEqual(answer["decision"], "no_bid")

    def test_marking_turns_grow_with_the_leftover_time(self):
        self.assertEqual(marking_turns(90), 28)
        self.assertEqual(marking_turns(600), 50)
        self.assertEqual(marking_turns(2000), 80)


if __name__ == "__main__":
    unittest.main()
