"""RED tests for server/envelopes.py — classify-phase canonical job records.

Contract: knowledge/a1.md pins the three sanctioned board connectors;
knowledge/a2.md pins the per-source first-published signals (Lever createdAt
is epoch MILLISECONDS) and honest provenance (raw vendor dict retained).
All payloads are hand-written golden vendor samples; no network.

A GREEN here before the parser existed would itself be a failure.
"""

import unittest

from server import envelopes

# 2024-01-15T10:00:00Z
ASHBY_PUBLISHED_EPOCH_S = 1705312800.0
# 2024-01-15T00:00:00Z (greenhouse date-only golden)
GREENHOUSE_DATE_ONLY_EPOCH_S = 1705276800.0
# 2024-01-15T09:15:00Z
GREENHOUSE_FULL_ISO_EPOCH_S = 1705310100.0
# 2024-01-16T08:00:00Z (naive ISO treated as UTC)
GREENHOUSE_NAIVE_ISO_EPOCH_S = 1705392000.0
# 1700000000000 ms
LEVER_CREATED_EPOCH_S = 1700000000.0

CANONICAL_KEYS = frozenset({
    "source_id", "raw_id", "title", "employer", "location", "url",
    "first_published_field", "first_published_epoch_s", "fetched_at_epoch_s",
    "raw",
})

ASHBY_GOLDEN = {
    "jobs": [
        {
            "id": "job-uuid-1",
            "title": "Senior Backend Engineer",
            "location": "Berlin, DE",
            "jobUrl": "https://jobs.ashbyhq.com/acme/job-uuid-1",
            "applyUrl": "https://jobs.ashbyhq.com/acme/job-uuid-1/application",
            "publishedAt": "2024-01-15T10:00:00Z",
            "isListed": True,
        },
        {
            "id": "job-uuid-2",
            "title": "Support Engineer",
            "location": None,
            "jobUrl": "https://jobs.ashbyhq.com/acme/job-uuid-2",
            "publishedAt": "2024-02-01T08:30:00Z",
            "isListed": True,
        },
    ],
}

GREENHOUSE_GOLDEN = {
    "jobs": [
        {
            "id": 123456789,
            "internal_job_id": 987654321,
            "title": "Staff Platform Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/123456789",
            "location": {"name": "Amsterdam"},
            "metadata": [
                {"name": "first_published", "value": "2024-01-15T09:15:00Z",
                 "value_type": "datetime"},
            ],
        },
    ],
}

LEVER_GOLDEN = [
    {
        "id": "posting-1",
        "text": "Data Engineer",
        "hostedUrl": "https://jobs.lever.co/acme/posting-1",
        "applyUrl": "https://jobs.lever.co/acme/posting-1/apply",
        "createdAt": 1700000000000,
        "categories": {"location": "Remote", "team": "Data"},
    },
]


class EnvelopeParserTests(unittest.TestCase):
    def test_ashby_happy_path_builds_canonical_records(self):
        records = envelopes.parse_ashby(
            ASHBY_GOLDEN, "ashby:acme", "Acme", 1709999999.5)
        self.assertEqual(len(records), 2)
        rec = records[0]
        self.assertEqual(set(rec), set(CANONICAL_KEYS) | {"is_listed"})
        self.assertEqual(rec["source_id"], "ashby:acme")
        self.assertEqual(rec["raw_id"], "job-uuid-1")
        self.assertEqual(rec["title"], "Senior Backend Engineer")
        self.assertEqual(rec["employer"], "Acme")
        self.assertEqual(rec["location"], "Berlin, DE")
        self.assertEqual(rec["url"],
                         "https://jobs.ashbyhq.com/acme/job-uuid-1")
        self.assertEqual(rec["first_published_field"], "publishedAt")
        self.assertEqual(rec["first_published_epoch_s"],
                         ASHBY_PUBLISHED_EPOCH_S)
        self.assertEqual(rec["fetched_at_epoch_s"], 1709999999.5)
        self.assertIs(rec["is_listed"], True)
        self.assertIs(rec["raw"], ASHBY_GOLDEN["jobs"][0])
        rec2 = records[1]
        self.assertIsNone(rec2["location"])
        self.assertEqual(rec2["first_published_epoch_s"], 1706776200.0)
        self.assertIs(rec2["raw"], ASHBY_GOLDEN["jobs"][1])

    def test_ashby_unparseable_published_at_is_none(self):
        payload = {"jobs": [
            {"id": "a", "title": "T1", "jobUrl": "u1",
             "publishedAt": "not-a-date", "isListed": True},
            {"id": "b", "title": "T2", "jobUrl": "u2",
             "publishedAt": "10:00:00", "isListed": True},
        ]}
        records = envelopes.parse_ashby(payload, "ashby:acme", "Acme", 1.0)
        self.assertIsNone(records[0]["first_published_epoch_s"])
        self.assertIsNone(records[1]["first_published_epoch_s"])

    def test_ashby_missing_published_at_is_none(self):
        payload = {"jobs": [
            {"id": "a", "title": "T1", "jobUrl": "u1", "isListed": True},
        ]}
        records = envelopes.parse_ashby(payload, "ashby:acme", "Acme", 1.0)
        self.assertIsNone(records[0]["first_published_epoch_s"])

    def test_ashby_omits_is_listed_key_when_field_absent(self):
        payload = {"jobs": [
            {"id": "a", "title": "T1", "jobUrl": "u1",
             "publishedAt": "2024-01-15T10:00:00Z"},
            {"id": "b", "title": "T2", "jobUrl": "u2",
             "publishedAt": "2024-01-15T10:00:00Z", "isListed": None},
        ]}
        records = envelopes.parse_ashby(payload, "ashby:acme", "Acme", 1.0)
        self.assertNotIn("is_listed", records[0])
        self.assertIn("is_listed", records[1])
        self.assertIsNone(records[1]["is_listed"])

    def test_ashby_url_falls_back_to_apply_url(self):
        payload = {"jobs": [
            {"id": "a", "title": "T1", "applyUrl": "https://x/apply",
             "publishedAt": "2024-01-15T10:00:00Z", "isListed": True},
            {"id": "b", "title": "T2",
             "publishedAt": "2024-01-15T10:00:00Z", "isListed": True},
        ]}
        records = envelopes.parse_ashby(payload, "ashby:acme", "Acme", 1.0)
        self.assertEqual(records[0]["url"], "https://x/apply")
        self.assertIsNone(records[1]["url"])

    def test_ashby_skips_rows_without_id_or_title(self):
        payload = {"jobs": [
            {"title": "No id", "jobUrl": "u"},
            {"id": "c", "jobUrl": "u"},
            {"id": "", "title": "Empty id", "jobUrl": "u"},
            {"id": "d", "title": "", "jobUrl": "u"},
            "not-a-dict",
            {"id": "e", "title": "Keep", "jobUrl": "u",
             "publishedAt": "2024-01-15T10:00:00Z", "isListed": True},
        ]}
        records = envelopes.parse_ashby(payload, "ashby:acme", "Acme", 1.0)
        self.assertEqual([r["raw_id"] for r in records], ["e"])

    def test_ashby_non_dict_payload_raises_envelope_error(self):
        with self.assertRaises(envelopes.EnvelopeError):
            envelopes.parse_ashby(["not", "a", "dict"], "ashby:acme", "Acme", 1.0)

    def test_ashby_missing_jobs_raises_envelope_error(self):
        with self.assertRaises(envelopes.EnvelopeError):
            envelopes.parse_ashby({"meta": {"board": "acme"}}, "ashby:acme", "Acme", 1.0)

    def test_greenhouse_happy_path_builds_canonical_record(self):
        records = envelopes.parse_greenhouse(
            GREENHOUSE_GOLDEN, "greenhouse:acme", "Acme", 1709999999.5)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(set(rec), set(CANONICAL_KEYS) | {"internal_job_id"})
        self.assertEqual(rec["source_id"], "greenhouse:acme")
        self.assertEqual(rec["raw_id"], "123456789")
        self.assertEqual(rec["title"], "Staff Platform Engineer")
        self.assertEqual(rec["employer"], "Acme")
        self.assertEqual(rec["location"], "Amsterdam")
        self.assertEqual(rec["url"],
                         "https://boards.greenhouse.io/acme/jobs/123456789")
        self.assertEqual(rec["first_published_field"], "first_published")
        self.assertEqual(rec["first_published_epoch_s"],
                         GREENHOUSE_FULL_ISO_EPOCH_S)
        self.assertEqual(rec["fetched_at_epoch_s"], 1709999999.5)
        self.assertEqual(rec["internal_job_id"], "987654321")
        self.assertIs(rec["raw"], GREENHOUSE_GOLDEN["jobs"][0])

    def test_greenhouse_date_only_and_naive_iso_first_published(self):
        payload = {"jobs": [
            {"id": 1, "title": "T1", "absolute_url": "u1",
             "location": {"name": "X"}, "internal_job_id": None,
             "metadata": [{"name": "first_published", "value": "2024-01-15",
                           "value_type": "date"}]},
            {"id": 2, "title": "T2", "absolute_url": "u2",
             "location": {"name": "X"}, "internal_job_id": None,
             "metadata": [{"name": "first_published",
                           "value": "2024-01-16T08:00:00",
                           "value_type": "datetime"}]},
        ]}
        records = envelopes.parse_greenhouse(
            payload, "greenhouse:acme", "Acme", 1.0)
        self.assertEqual(records[0]["first_published_epoch_s"],
                         GREENHOUSE_DATE_ONLY_EPOCH_S)
        self.assertEqual(records[1]["first_published_epoch_s"],
                         GREENHOUSE_NAIVE_ISO_EPOCH_S)

    def test_greenhouse_missing_or_malformed_metadata_gives_none(self):
        payload = {"jobs": [
            {"id": 1, "title": "T1", "absolute_url": "u1",
             "location": {"name": "X"}, "internal_job_id": None},
            {"id": 2, "title": "T2", "absolute_url": "u2",
             "location": {"name": "X"}, "internal_job_id": None,
             "metadata": None},
            {"id": 3, "title": "T3", "absolute_url": "u3",
             "location": {"name": "X"}, "internal_job_id": None,
             "metadata": [42, {"name": "updated_at",
                               "value": "2024-01-01T00:00:00Z"}]},
        ]}
        records = envelopes.parse_greenhouse(
            payload, "greenhouse:acme", "Acme", 1.0)
        for rec in records:
            self.assertIsNone(rec["first_published_epoch_s"])

    def test_greenhouse_omits_internal_job_id_key_when_field_absent(self):
        payload = {"jobs": [
            {"id": 1, "title": "T1", "absolute_url": "u1",
             "location": {"name": "X"},
             "metadata": [{"name": "first_published", "value": "2024-01-15",
                           "value_type": "date"}]},
            {"id": 2, "title": "T2", "absolute_url": "u2",
             "internal_job_id": None, "location": {"name": "X"},
             "metadata": [{"name": "first_published", "value": "2024-01-15",
                           "value_type": "date"}]},
        ]}
        records = envelopes.parse_greenhouse(
            payload, "greenhouse:acme", "Acme", 1.0)
        self.assertNotIn("internal_job_id", records[0])
        self.assertIn("internal_job_id", records[1])
        self.assertIsNone(records[1]["internal_job_id"])

    def test_greenhouse_skips_rows_without_id_or_title(self):
        payload = {"jobs": [
            {"title": "No id", "absolute_url": "u"},
            {"id": 1, "absolute_url": "u"},
            {"id": "", "title": "T", "absolute_url": "u"},
            "not-a-dict",
            {"id": 5, "title": "Keep", "absolute_url": "u",
             "location": {"name": "X"}, "internal_job_id": None,
             "metadata": [{"name": "first_published", "value": "2024-01-15",
                           "value_type": "date"}]},
        ]}
        records = envelopes.parse_greenhouse(
            payload, "greenhouse:acme", "Acme", 1.0)
        self.assertEqual([r["raw_id"] for r in records], ["5"])

    def test_greenhouse_missing_location_is_none(self):
        payload = {"jobs": [
            {"id": 1, "title": "T1", "absolute_url": "u1",
             "internal_job_id": None,
             "metadata": [{"name": "first_published", "value": "2024-01-15",
                           "value_type": "date"}]},
        ]}
        records = envelopes.parse_greenhouse(
            payload, "greenhouse:acme", "Acme", 1.0)
        self.assertIsNone(records[0]["location"])

    def test_greenhouse_non_dict_payload_raises_envelope_error(self):
        with self.assertRaises(envelopes.EnvelopeError):
            envelopes.parse_greenhouse([{"jobs": []}], "greenhouse:acme", "Acme", 1.0)

    def test_greenhouse_missing_jobs_raises_envelope_error(self):
        with self.assertRaises(envelopes.EnvelopeError):
            envelopes.parse_greenhouse({"jobs": "nope"}, "greenhouse:acme", "Acme", 1.0)

    def test_lever_epoch_ms_becomes_seconds_and_builds_canonical_record(self):
        records = envelopes.parse_lever(
            LEVER_GOLDEN, "lever:acme", "Acme", 1709999999.5)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(set(rec), set(CANONICAL_KEYS))
        self.assertEqual(rec["source_id"], "lever:acme")
        self.assertEqual(rec["raw_id"], "posting-1")
        self.assertEqual(rec["title"], "Data Engineer")
        self.assertEqual(rec["employer"], "Acme")
        self.assertEqual(rec["location"], "Remote")
        self.assertEqual(rec["url"], "https://jobs.lever.co/acme/posting-1")
        self.assertEqual(rec["first_published_field"], "createdAt")
        self.assertEqual(rec["first_published_epoch_s"], LEVER_CREATED_EPOCH_S)
        self.assertEqual(rec["fetched_at_epoch_s"], 1709999999.5)
        self.assertIs(rec["raw"], LEVER_GOLDEN[0])

    def test_lever_url_falls_back_to_apply_url(self):
        payload = [
            {"id": "a", "text": "T1",
             "applyUrl": "https://jobs.lever.co/acme/posting-9/apply",
             "createdAt": 1700000000000, "categories": {"location": "X"}},
            {"id": "b", "text": "T2", "createdAt": 1700000000000,
             "categories": {"location": "X"}},
        ]
        records = envelopes.parse_lever(payload, "lever:acme", "Acme", 1.0)
        self.assertIsNone(records[1]["url"])
        self.assertEqual(records[0]["url"],
                         "https://jobs.lever.co/acme/posting-9/apply")

    def test_lever_skips_rows_without_id_or_text(self):
        payload = [
            {"text": "No id", "hostedUrl": "u"},
            {"id": "a", "hostedUrl": "u"},
            {"id": "", "text": "T", "hostedUrl": "u"},
            {"id": "b", "text": "", "hostedUrl": "u"},
            "not-a-dict",
            {"id": "c", "text": "Keep", "hostedUrl": "u",
             "createdAt": 1700000000000, "categories": {"location": "X"}},
        ]
        records = envelopes.parse_lever(payload, "lever:acme", "Acme", 1.0)
        self.assertEqual([r["raw_id"] for r in records], ["c"])

    def test_lever_missing_or_unparseable_created_at_is_none(self):
        payload = [
            {"id": "a", "text": "T1", "hostedUrl": "u1",
             "categories": {"location": "X"}},
            {"id": "b", "text": "T2", "hostedUrl": "u2",
             "createdAt": "not-a-number", "categories": {"location": "X"}},
        ]
        records = envelopes.parse_lever(payload, "lever:acme", "Acme", 1.0)
        for posting_rec in records:
            self.assertIsNone(posting_rec["first_published_epoch_s"])
        self.assertEqual(posting_rec["first_published_field"], "createdAt")

    def test_lever_missing_location_is_none(self):
        payload = [{"id": "a", "text": "T1", "hostedUrl": "u1",
                    "createdAt": 1700000000000}]
        records = envelopes.parse_lever(payload, "lever:acme", "Acme", 1.0)
        rec = records[0]
        self.assertIsNone(rec["location"])

    def test_lever_non_list_payload_raises_envelope_error(self):
        with self.assertRaises(envelopes.EnvelopeError):
            envelopes.parse_lever({"jobs": []}, "lever:acme", "Acme", 1.0)
