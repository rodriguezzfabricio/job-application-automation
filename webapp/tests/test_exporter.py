"""RED tests — CSV export builder (Phase 9 tracking, scar P9).

knowledge/a6.md Q21 contract: the export enumerates EVERY tracked field.
The applications CSV carries exactly the 13 Q21 columns in schema order —
`updated_at` included (the recorded 12-vs-13 defect) — and
`status_history.csv` carries exactly `id, application_id, status, changed_at`
ordered by `changed_at` ascending. The builder is pure: dicts in, CSV text
out; no db, no I/O. These tests pin the export schema at module level; the
endpoint-level P9 scar test (`test_export_endpoint_covers_every_tracked_field`)
is owned by the parent.

What these tests guarantee:
- If the header ever drops, renames, reorders, or adds an applications
  column, the export tests fail (P9: every tracked field exported).
- If `updated_at` ever disappears from the export, the 12-vs-13 regression
  pin fails.
- If history is ever emitted in the wrong order or with the wrong columns,
  the history tests fail.
- If any value containing commas, quotes, or newlines is corrupted on the
  way through the CSV, the round-trip test fails.
- If a row missing keys ever crashes the builder or fabricates a value, the
  missing-keys test fails.
"""

import csv
import io
import unittest

from server import exporter

SCHEMA_13 = (
    "canonical_job_id",
    "employer",
    "title",
    "canonical_apply_url",
    "status",
    "saved_at",
    "applied_at",
    "status_changed_at",
    "updated_at",
    "notes",
    "cover_letter_ref",
    "tailored_resume_ref",
    "fact_store_version",
)

HISTORY_4 = ("id", "application_id", "status", "changed_at")


class ApplicationsCsv(unittest.TestCase):
    def test_columns_exact_13_in_schema_order(self):
        # P9 12-vs-13 regression pin: updated_at MUST be included, in place.
        header = exporter.build_applications_csv([]).splitlines()[0]
        self.assertEqual(next(csv.reader(io.StringIO(header))), list(SCHEMA_13))

    def test_missing_keys_become_empty_strings(self):
        # A sparse store row must never crash the builder nor fabricate data.
        text = exporter.build_applications_csv(
            [{"canonical_job_id": "ashby:acme:job-1"}]
        )
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[1][0], "ashby:acme:job-1")
        self.assertEqual(rows[1][1:], [""] * 12)
        # None values normalize to the empty string as well.
        text = exporter.build_applications_csv(
            [{"canonical_job_id": "a", "notes": None}]
        )
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[1][SCHEMA_13.index("notes")], "")
        # Extra store-internal fields never leak into the export.
        text = exporter.build_applications_csv(
            [{"canonical_job_id": "a", "internal_token": "secret"}]
        )
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[1], ["a"] + [""] * 12)

    def test_quoting_roundtrip(self):
        original = 'Said, "hi"\nLine two'
        text = exporter.build_applications_csv(
            [{"canonical_job_id": "a", "notes": original}]
        )
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[1][SCHEMA_13.index("notes")], original)

    def test_no_column_drift(self):
        # Schema-level conformance, in-module: the pinned tuple must have
        # exactly 13 entries and exactly the Q21 names.
        self.assertEqual(len(exporter.APPLICATIONS_COLUMNS), 13)
        self.assertEqual(set(exporter.APPLICATIONS_COLUMNS), set(SCHEMA_13))


class HistoryCsv(unittest.TestCase):
    def test_history_columns_exact_4_in_order(self):
        header = exporter.build_history_csv([]).splitlines()[0]
        self.assertEqual(next(csv.reader(io.StringIO(header))), list(HISTORY_4))
        self.assertEqual(exporter.HISTORY_COLUMNS, HISTORY_4)

    def test_history_ordered_by_changed_at(self):
        rows = [
            {"id": 3, "application_id": "j1", "status": "Offer",
             "changed_at": "2026-01-03T00:00:00Z"},
            {"id": 1, "application_id": "j1", "status": "Saved",
             "changed_at": "2026-01-01T00:00:00Z"},
            {"id": 2, "application_id": "j1", "status": "Applied",
             "changed_at": "2026-01-02T00:00:00Z"},
        ]
        snapshot = list(rows)
        parsed = list(csv.reader(io.StringIO(exporter.build_history_csv(rows))))
        self.assertEqual([r[0] for r in parsed[1:]], ["1", "2", "3"])
        # Pure function: the caller's list is left untouched.
        self.assertEqual(rows, snapshot)


class BuildExport(unittest.TestCase):
    def test_build_export_returns_both_artifacts(self):
        apps = [{"canonical_job_id": "a", "status": "Saved"}]
        history = [
            {"id": 1, "application_id": "a", "status": "Saved",
             "changed_at": "2026-01-01T00:00:00Z"},
        ]
        artifacts = exporter.build_export(apps, history)
        self.assertEqual(
            set(artifacts), {"applications.csv", "status_history.csv"}
        )
        self.assertEqual(
            artifacts["applications.csv"], exporter.build_applications_csv(apps)
        )
        self.assertEqual(
            artifacts["status_history.csv"], exporter.build_history_csv(history)
        )


if __name__ == "__main__":
    unittest.main()
