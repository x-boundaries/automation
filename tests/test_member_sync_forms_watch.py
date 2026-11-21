"""#155 FH1c: forms watch fixes before switch-on (G2 #320 LATER LA, LC, LB).

Offline, fakes only, as tests/test_member_sync.py (whose forms fakes and
helpers this file reuses): no Shopify, AutoCount or network call. Fake phone
numbers in the reserved range and example.com emails only.

- LA: a record refused only because its ``submitted_at`` is more than a day
  ahead is counted once as ``forms_record_invalid``, also after its time
  comes within range.
- LC: a kept read position whose first page comes back empty with no next
  page is not a complete read (the read day does not move; the position is
  cleared and the day is read again).
- LB: a run from a kept position that moves forward and then fails keeps
  its new position.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "directlocal"))
sys.path.insert(0, str(ROOT / "tests"))

import xb_directlocal as dl  # noqa: E402
import xb_member_sync as sync  # noqa: E402
import test_member_sync as ms  # noqa: E402


class EndAtOnceShopify(ms.FormsShopify):
    """FormsShopify whose answer to a cursor in ``dead`` is an empty last page with no error
    (the undocumented case G2 LC describes), whatever customers follow it."""

    def __init__(self):
        super().__init__()
        self.dead: set[str] = set()

    def forms_page(self, search, after):
        if after is not None and after in self.dead:
            self.forms_searches.append((search, after))
            return [], False
        return super().forms_page(search, after)


class FormsFh1cTestCase(ms.FormsTestCase):
    def setUp(self):
        super().setUp()
        self.shopify = EndAtOnceShopify()

    def cursor(self):
        raw = self.ledger.watermark(sync.FORMS_CURSOR_MARK)
        return None if raw is None else json.loads(raw)


class FarAheadRecordTests(FormsFh1cTestCase):
    """G2 LA: before FH1c a far-ahead record was marked by the digest of its raw values, then
    classified and counted again under its submission id once its time came within a day."""

    def far_ahead(self, n: int = 1, **record):
        values = {"submitted_at": dl.ts(self.now + timedelta(days=1, hours=1)), **record}
        return ms.forms_customer(n, record=values)

    def test_a_far_ahead_record_is_counted_once_when_its_time_comes_in_range(self):
        self.shopify.forms = [self.far_ahead()]
        counts = self.watch()
        self.assertEqual((counts["forms_record_invalid"], counts["forms_records_new"]), (1, 1))
        self.now += timedelta(hours=2)  # the same values, now within a day: readable
        counts = self.watch()
        self.assertEqual((counts["forms_records_seen"], counts["forms_records_new"]), (1, 0))
        self.assertFalse(set(counts) & set(sync.FORMS_OUTCOME_CODES), counts)
        self.assertFalse(set(counts) & set(sync.FORMS_CLASS_CODES), counts)
        (value,) = self.marks().values()
        self.assertTrue(value.startswith("forms_record_invalid|"), value)

    def test_a_corrected_time_on_the_same_submission_is_not_counted_again(self):
        self.shopify.forms = [self.far_ahead()]
        self.assertEqual(self.watch()["forms_record_invalid"], 1)
        self.shopify.forms = [ms.forms_customer(1, record={"submitted_at": dl.ts(self.now)})]  # the Flow fixed the time
        counts = self.watch()
        self.assertEqual(counts["forms_records_new"], 0)
        self.assertEqual(len(self.marks()), 1)

    def test_the_far_ahead_mark_is_named_by_the_submission_id(self):
        self.shopify.forms = [self.far_ahead()]
        self.watch()
        self.shopify.forms = [ms.forms_customer(1, record={"submitted_at": dl.ts(self.now)})]
        engine = self.forms_engine()
        record = sync.parse_forms_record(self.shopify.forms[0].metafields)
        self.assertIsNotNone(record)
        self.assertEqual(set(self.marks()), {sync.FORMS_SEEN_PREFIX + engine._forms_key(self.shopify.forms[0], record)})

    def test_another_far_ahead_submission_is_counted_on_its_own(self):
        self.shopify.forms = [self.far_ahead()]
        self.watch()
        self.shopify.forms = [self.far_ahead(1, submission_id="sub-0777")]  # a new submission, same customer
        self.assertEqual(self.watch()["forms_record_invalid"], 1)
        self.assertEqual(len(self.marks()), 2)

    def test_the_far_ahead_mark_is_never_pruned(self):
        self.shopify.forms = [self.far_ahead()]
        self.watch()
        self.now += timedelta(days=200)
        self.watch()  # the prune runs at the start of every step
        (value,) = self.marks().values()
        self.assertTrue(value.startswith("forms_record_invalid|"), value)

    def test_exactly_a_day_ahead_is_still_classified(self):
        self.shopify.forms = [ms.forms_customer(1, record={"submitted_at": dl.ts(self.now + timedelta(days=1))})]
        counts = self.watch()
        self.assertEqual((counts["forms_would_heal"], counts.get("forms_record_invalid")), (1, None))


class KeptPositionAnsweredWithTheEndTests(FormsFh1cTestCase):
    """G2 LC (P4): a kept position that Shopify answers at once with an empty last page and no
    error. Before FH1c this read as complete, the read day moved, and the customers after the
    position (updated before the new read day) were never read."""

    def test_the_read_day_does_not_move_and_the_rest_is_read_next_run(self):
        self.shopify.forms = [ms.forms_customer(n) for n in range(1, 56)]
        counts = self.watch(forms_watch_max_pages=1)
        self.assertEqual(counts["forms_records_new"], 50)
        kept = self.cursor()["after"]
        self.shopify.dead = {kept}
        self.now += timedelta(days=2)  # a moved read day would leave customers 51-55 behind
        counts = self.watch(forms_watch_max_pages=1)
        self.assertEqual(self.shopify.forms_searches[-1][1], kept)
        self.assertEqual((counts["forms_read_incomplete"], counts["forms_read_stalled"], counts["forms_records_new"]), (1, 1, 0))
        self.assertIsNone(self.ledger.watermark(sync.FORMS_WATERMARK))
        self.assertIsNone(self.cursor())
        self.shopify.dead = set()
        counts = self.watch(forms_watch_max_pages=2)  # the day again from its start
        self.assertIsNone(self.shopify.forms_searches[-2][1])
        self.assertEqual((counts["forms_records_seen"], counts["forms_records_new"]), (55, 5))  # 51-55, once each
        self.assertNotIn("forms_read_incomplete", counts)
        self.assertEqual(len(self.marks()), 55)

    def test_an_empty_first_page_with_no_kept_position_is_a_complete_read(self):
        self.shopify.forms = []
        counts = self.watch()
        self.assertNotIn("forms_read_incomplete", counts)
        self.assertEqual(self.ledger.watermark(sync.FORMS_WATERMARK), "2026-10-22")

    def test_a_kept_position_followed_by_customers_is_read_as_before(self):
        self.shopify.forms = [ms.forms_customer(n) for n in range(1, 56)]
        self.watch(forms_watch_max_pages=1)
        counts = self.watch(forms_watch_max_pages=1)
        self.assertEqual(counts["forms_records_new"], 5)
        self.assertNotIn("forms_read_incomplete", counts)
        self.assertIsNone(self.cursor())


class KeptPositionProgressThenFailureTests(FormsFh1cTestCase):
    """G2 LB (survivor X2): ``test_a_failure_after_some_pages_keeps_the_progress`` starts with no
    kept position. Here a run starts from one, moves forward a page, then a page fails."""

    def test_the_new_position_is_kept_and_nothing_is_stalled(self):
        self.shopify.page_size = 20
        self.shopify.forms = [ms.forms_customer(n) for n in range(1, 71)]
        self.watch(forms_watch_max_pages=1)
        self.assertEqual(self.cursor(), {"day": ms.FORMS_SINCE, "after": "19.0|" + ms.gid(20)})
        self.shopify.forms_error = len(self.shopify.forms_searches) + 2  # this run's second page fails
        counts = self.watch()
        self.assertEqual(self.shopify.forms_searches[-2][1], "19.0|" + ms.gid(20))
        self.assertEqual((counts["forms_records_new"], counts["forms_read_incomplete"]), (20, 1))
        self.assertNotIn("forms_read_stalled", counts)
        self.assertEqual(self.cursor(), {"day": ms.FORMS_SINCE, "after": "39.0|" + ms.gid(40)})
        self.shopify.forms_error = False
        counts = self.watch()
        self.assertEqual(self.shopify.forms_searches[-2][1], "39.0|" + ms.gid(40))
        self.assertEqual((counts["forms_records_seen"], counts["forms_records_new"]), (30, 30))  # 41-70, once each
        self.assertEqual(len(self.marks()), 70)


if __name__ == "__main__":
    unittest.main()
