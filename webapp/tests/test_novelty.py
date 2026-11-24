"""Novelty & freshness predicate tests (a2 freshness contract, a1 raw-id unseen)."""

import unittest

from server import novelty


def make_record(**overrides):
    """Canonical record dict; overrides replace individual fields."""
    record = {
        "source_id": "ashby:acme",
        "raw_id": "ashby:acme:uuid-1",
        "title": "Senior Backend Engineer",
        "employer": "Acme Corp",
        "location": None,
        "url": None,
        "first_published_field": "publishedAt",
        "first_published_epoch_s": 1000.0,
        "fetched_at_epoch_s": 2000.0,
    }
    record.update(overrides)
    return record


class NoveltyConstants(unittest.TestCase):
    def test_module_constants_match_a2_pins(self):
        self.assertEqual(novelty.BOOTSTRAP_WINDOW_S, 3600)
        self.assertEqual(novelty.WATERMARK_AGE_S, 30 * 86400)


class NormalizeTitle(unittest.TestCase):
    def test_normalize_title_collapses_whitespace_and_case(self):
        self.assertEqual(
            novelty.normalize_title("  Senior  Backend\n Engineer "),
            "senior backend engineer",
        )


class FirstPublishedAge(unittest.TestCase):
    def test_age_renders_from_posting_signal_not_fetched_at(self):
        # a2: posting age renders from the posting's OWN field, never seen-at.
        now = 100000.0
        record = make_record(
            first_published_epoch_s=90000.0, fetched_at_epoch_s=60000.0
        )
        self.assertEqual(novelty.first_published_age_s(record, now), 10000.0)
        absent = make_record(first_published_epoch_s=None)
        self.assertIsNone(novelty.first_published_age_s(absent, now))


class IsFuture(unittest.TestCase):
    def test_future_true_past_false_absent_false(self):
        now = 1000.0
        self.assertTrue(
            novelty.is_future(make_record(first_published_epoch_s=1000.001), now)
        )
        self.assertFalse(
            novelty.is_future(make_record(first_published_epoch_s=1000.0), now)
        )
        self.assertFalse(
            novelty.is_future(make_record(first_published_epoch_s=None), now)
        )


class BootstrapWindow(unittest.TestCase):
    def test_golden_boundary_3600_in_3600_001_out(self):
        now = 10000.0
        at_edge = make_record(first_published_epoch_s=now - 3600)
        self.assertTrue(novelty.within_bootstrap_window(at_edge, now))
        just_out = make_record(first_published_epoch_s=now - 3600.001)
        self.assertFalse(novelty.within_bootstrap_window(just_out, now))

    def test_absent_signal_not_within_window(self):
        absent = make_record(first_published_epoch_s=None)
        self.assertFalse(novelty.within_bootstrap_window(absent, 10000.0))

    def test_future_signal_refused_by_window(self):
        now = 10000.0
        future = make_record(first_published_epoch_s=now + 1)
        self.assertTrue(novelty.is_future(future, now))
        self.assertFalse(novelty.within_bootstrap_window(future, now))


class IsNew(unittest.TestCase):
    NOW = 10000.0

    def test_unseen_raw_id_within_window_is_new(self):
        record = make_record(first_published_epoch_s=self.NOW - 100)
        self.assertTrue(novelty.is_new(record, set(), self.NOW))

    def test_raw_id_seen_is_not_new_even_inside_window(self):
        record = make_record(first_published_epoch_s=self.NOW - 100)
        self.assertFalse(novelty.is_new(record, {record["raw_id"]}, self.NOW))

    def test_absent_signal_is_never_new(self):
        absent = make_record(first_published_epoch_s=None)
        self.assertFalse(novelty.is_new(absent, set(), self.NOW))

    def test_stale_31_day_signal_is_not_new_with_empty_seen_set(self):
        stale = make_record(first_published_epoch_s=self.NOW - 31 * 86400)
        self.assertFalse(novelty.is_new(stale, set(), self.NOW))


class PossibleStale(unittest.TestCase):
    NOW = 10000.0

    def test_31_day_old_signal_is_possible_stale(self):
        stale = make_record(first_published_epoch_s=self.NOW - 31 * 86400)
        self.assertTrue(novelty.possible_stale(stale, self.NOW))

    def test_exactly_watermark_age_is_not_stale(self):
        at_edge = make_record(first_published_epoch_s=self.NOW - 30 * 86400)
        self.assertFalse(novelty.possible_stale(at_edge, self.NOW))

    def test_future_and_absent_signals_are_not_stale(self):
        future = make_record(first_published_epoch_s=self.NOW + 1)
        absent = make_record(first_published_epoch_s=None)
        self.assertFalse(novelty.possible_stale(future, self.NOW))
        self.assertFalse(novelty.possible_stale(absent, self.NOW))


class RepublishCandidate(unittest.TestCase):
    NOW = 100000.0

    def test_same_normalized_title_employer_seen_10_days_ago_is_candidate(self):
        record = make_record(title="  Senior  Backend\n Engineer ", employer="ACME CORP")
        seen_titles = {"senior backend engineer|acme corp": self.NOW - 10 * 86400}
        self.assertTrue(novelty.republish_candidate(record, seen_titles, self.NOW))

    def test_different_employer_is_not_candidate(self):
        record = make_record(employer="Globex Inc")
        seen_titles = {"senior backend engineer|acme corp": self.NOW - 10 * 86400}
        self.assertFalse(novelty.republish_candidate(record, seen_titles, self.NOW))

    def test_beyond_horizon_is_not_candidate(self):
        record = make_record()
        seen_titles = {"senior backend engineer|acme corp": self.NOW - 31 * 86400}
        self.assertFalse(novelty.republish_candidate(record, seen_titles, self.NOW))

    def test_unseen_key_is_not_candidate(self):
        record = make_record()
        self.assertFalse(novelty.republish_candidate(record, {}, self.NOW))

    def test_future_last_seen_is_not_candidate(self):
        record = make_record()
        seen_titles = {"senior backend engineer|acme corp": self.NOW + 10}
        self.assertFalse(novelty.republish_candidate(record, seen_titles, self.NOW))


if __name__ == "__main__":
    unittest.main()
