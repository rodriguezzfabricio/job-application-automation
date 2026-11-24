"""RED tests — poller ingestion wiring (fetch success -> ingest_snapshot +
feed-state classification, MIGRATION-002 persistence).

The Phase-1 poller only recorded politeness facts; a successful fetch was
thrown away. This slice pins poll_board: one poll classifies the outcome into
the feed_status state machine, ingests the body on 2xx, and persists the four
MIGRATION-002 counters (schema_fingerprint, consecutive_invalid,
consecutive_http_404, consecutive_empty, dead) back onto the sources row.
"""

import json
import os
import tempfile
import unittest

from server import db, feed_status, poller


NOW = 1_700_000_000.0


def _ashby_body(rows=1, title="Backend Engineer"):
    return json.dumps({
        "jobs": [
            {
                "id": f"uuid-{i}",
                "title": title,
                "jobUrl": f"https://jobs.acme.example/uuid-{i}",
                "publishedAt": "2023-11-14T22:11:00Z",
            }
            for i in range(rows)
        ],
    })


class StubFetcher:
    """Test-only duck fetcher: returns canned (status, body) per call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def poll(self, row, timeout_s):
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[idx]


class PollBoardIngest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = db.Store(os.path.join(self.tmpdir.name, "t.sqlite"))
        self.addCleanup(self.tmpdir.cleanup)
        self.store.add_source({
            "id": "ashby:acme", "name": "Acme", "kind": "ashby",
            "host": "api.ashbyhq.com",
            "feed_url": "https://api.ashbyhq.com/posting-api/job-board/acme",
        })
        self.row = self.store.get_source("ashby:acme")

    def test_success_poll_ingests_jobs_and_records_clean_feed_state(self):
        fetcher = StubFetcher([(200, _ashby_body())])
        _status, state, _summary = poller.poll_board(self.store, fetcher, self.row, NOW)
        self.assertEqual(len(self.store.list_jobs()), 1)
        self.assertEqual(state["consecutive_invalid"], 0)
        self.assertEqual(state["consecutive_http_404"], 0)
        self.assertEqual(state["consecutive_empty"], 0)
        fresh = self.store.get_source("ashby:acme")
        self.assertEqual(fresh["consecutive_invalid"], 0)
        self.assertEqual(fresh["consecutive_http_404"], 0)
        self.assertEqual(fresh["consecutive_empty"], 0)
        self.assertEqual(fresh["dead"], 0)

    def test_invalid_body_counts_invalid_envelope_without_crashing(self):
        fetcher = StubFetcher([(200, b"{not json")])
        _status, state, _summary = poller.poll_board(self.store, fetcher, self.row, NOW)
        self.assertEqual(state["consecutive_invalid"], 1)
        self.assertEqual(
            self.store.get_source("ashby:acme")["consecutive_invalid"], 1)

    def test_empty_valid_body_counts_empty(self):
        fetcher = StubFetcher([(200, json.dumps({"jobs": []}))])
        _status, state, _summary = poller.poll_board(self.store, fetcher, self.row, NOW)
        self.assertEqual(state["consecutive_empty"], 1)
        self.assertEqual(
            self.store.get_source("ashby:acme")["consecutive_empty"], 1)

    def test_404_counts_http_404(self):
        fetcher = StubFetcher([(404, b"")])
        _status, state, _summary = poller.poll_board(self.store, fetcher, self.row, NOW)
        self.assertEqual(state["consecutive_http_404"], 1)
        self.assertEqual(
            self.store.get_source("ashby:acme")["consecutive_http_404"], 1)

    def test_three_404s_yield_a1_failure_message_never_dead(self):
        fetcher = StubFetcher([(404, b"")])
        state = None
        for _ in range(3):
            _status, state, _summary = poller.poll_board(self.store, fetcher, self.row, NOW)
        self.assertEqual(
            feed_status.feed_failure_message(state),
            "failed — suspected posting-API outage",
        )
        self.assertEqual(state["dead"], False)
        chip = feed_status.status_chip(state, NOW, 900.0, 1800.0)
        self.assertEqual(chip, "failed")

    def test_throttled_event_counts_429_in_state(self):
        fetcher = StubFetcher([(429, b"")])
        _status, state, _summary = poller.poll_board(self.store, fetcher, self.row, NOW)
        self.assertEqual(state["consecutive_429"], 1)
        self.assertEqual(state["paused"], False)  # 1 < PAUSE_AFTER_429


if __name__ == "__main__":
    unittest.main()
