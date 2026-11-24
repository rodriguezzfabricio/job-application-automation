"""P9 CSV export builders — pure functions; no db, no I/O.

Why this module exists: scar P9 requires the tracking store to be exportable
in open formats covering EVERY tracked field. knowledge/a6.md Q21 pins the two
artifacts: `applications.csv` with exactly the 13 Q21 columns in schema order
(`updated_at` included — the recorded 12-vs-13 defect), and
`status_history.csv` with `id, application_id, status, changed_at` ordered by
`changed_at` ascending.
"""

import csv
import io

APPLICATIONS_COLUMNS = (
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

HISTORY_COLUMNS = (
    "id",
    "application_id",
    "status",
    "changed_at",
)


def _cell(row, column):
    """One export cell; a missing key or None becomes the empty string."""
    value = row.get(column, "")
    return "" if value is None else value


def _render(columns, rows):
    # csv's QUOTE_MINIMAL quotes any field containing commas, quotes, or
    # newlines, so round-trips are lossless by construction.
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(row, column) for column in columns])
    return out.getvalue()


def build_applications_csv(rows):
    """Serialize application dicts to the 13-column Q21 CSV (header included)."""
    return _render(APPLICATIONS_COLUMNS, rows)


def build_history_csv(rows):
    """Serialize history dicts to CSV ordered by `changed_at` ascending."""
    # a6 Q21: ordering is by changed_at. The store emits ISO-8601 TEXT
    # timestamps, where lexicographic order is chronological order; the
    # string key also keeps the sort total (never crashes on mixed types).
    ordered = sorted(rows, key=lambda row: str(_cell(row, "changed_at")))
    return _render(HISTORY_COLUMNS, ordered)


def build_export(applications_rows, history_rows):
    """Both P9 artifacts in one dict, keyed by filename."""
    return {
        "applications.csv": build_applications_csv(applications_rows),
        "status_history.csv": build_history_csv(history_rows),
    }
