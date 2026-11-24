#!/usr/bin/env python3
"""Validate and append recruiter rows to stopgap-private/outreach/recruiters.csv.

Usage:
  python3 recruiters_append.py --check                 # validate the whole CSV, exit 1 on problems
  python3 recruiters_append.py rows.json               # append the rows in rows.json (a JSON list of objects)

Recruiter row validation:
  - linkedin_url is empty or a personal profile URL (https://www.linkedin.com/in/<handle>). Search URLs are refused.
  - a row with a linkedin_url needs verified_on (YYYY-MM-DD HH:MM ET): the time the profile page itself was opened
    and showed the company as current employer.
  - confidence is high, medium or low; a row without a profile URL is forced to low.
  - draft_message is at most 600 characters and never empty when a profile URL is present.
  - no duplicate (job_link, name); nobody from stopgap-private/do-not-contact.txt.
Old lines are never rewritten; rows are appended at the end.
"""
import csv
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(ROOT, "stopgap-private", "outreach", "recruiters.csv")
DNC_PATH = os.path.join(ROOT, "stopgap-private", "do-not-contact.txt")

COLUMNS = [
    "date_et", "company", "job_title", "job_link", "name", "role", "linkedin_url",
    "location", "evidence", "confidence", "found_via", "status", "verified_on", "draft_message",
]
PROFILE_RE = re.compile(r"^https://www\.linkedin\.com/in/[A-Za-z0-9._%-]+/?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VERIFIED_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


def load_dnc():
    names = set()
    if os.path.exists(DNC_PATH):
        with open(DNC_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip().lower()
                if line and not line.startswith("#"):
                    names.add(line)
    return names


def problems(row, existing_keys, dnc):
    out = []
    for c in ("date_et", "company", "job_title", "job_link", "name", "role"):
        if not (row.get(c) or "").strip():
            out.append(f"missing {c}")
    if not DATE_RE.match(row.get("date_et", "")):
        out.append("date_et must be YYYY-MM-DD")
    url = (row.get("linkedin_url") or "").strip()
    if url and not PROFILE_RE.match(url):
        out.append("linkedin_url is not a personal profile URL (linkedin.com/in/...)")
    if url and not VERIFIED_RE.match(row.get("verified_on", "")):
        out.append("verified_on (YYYY-MM-DD HH:MM) is required when linkedin_url is set")
    conf = (row.get("confidence") or "").strip().lower()
    if conf not in ("high", "medium", "low"):
        out.append("confidence must be high, medium or low")
    if not url and conf != "low":
        out.append("confidence must be low without a profile URL")
    msg = row.get("draft_message") or ""
    if len(msg) > 600:
        out.append("draft_message over 600 characters")
    if url and not msg.strip():
        out.append("draft_message is empty for a row with a profile URL")
    if (row.get("name") or "").strip().lower() in dnc:
        out.append("name is on do-not-contact.txt")
    key = (row.get("job_link", "").strip(), row.get("name", "").strip().lower())
    if key in existing_keys:
        out.append("duplicate of an existing row (same job_link and name)")
    return out


def read_existing():
    rows = []
    if os.path.exists(CSV_PATH):
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != COLUMNS:
                sys.exit(f"CSV header is {reader.fieldnames}, expected {COLUMNS}")
            rows = list(reader)
    return rows


def main(argv):
    dnc = load_dnc()
    existing = read_existing()
    if argv[:1] == ["--check"]:
        bad = unverified = 0
        keys = set()
        for i, row in enumerate(existing, start=2):
            p = problems(row, keys, dnc)
            keys.add((row["job_link"].strip(), row["name"].strip().lower()))
            if row.get("linkedin_url") and not row.get("verified_on"):
                unverified += 1
                p = [x for x in p if not x.startswith(("verified_on", "draft_message"))]
                print(f"line {i} {row['name']} @ {row['company']}: unverified (open the profile, then --set)")
            if p:
                bad += 1
                print(f"line {i} {row['name']} @ {row['company']}: " + "; ".join(p))
        print(f"{len(existing)} rows, {bad} with problems, {unverified} unverified")
        return 1 if bad else 0

    if argv[:1] == ["--set"]:
        # --set <job_link> <name> field=value ... : fills verification fields on one existing row.
        if len(argv) < 4:
            sys.exit("usage: --set <job_link> <name> field=value ...")
        allowed = {"linkedin_url", "verified_on", "draft_message", "status", "confidence", "role", "evidence"}
        job_link, name = argv[1].strip(), argv[2].strip().lower()
        updates = dict(a.split("=", 1) for a in argv[3:])
        if set(updates) - allowed:
            sys.exit(f"only these fields may change: {sorted(allowed)}")
        hit = None
        for row in existing:
            if row["job_link"].strip() == job_link and row["name"].strip().lower() == name:
                hit = row
                break
        if hit is None:
            sys.exit("no row with that job_link and name")
        trial = dict(hit)
        trial.update(updates)
        p = problems(trial, set(), dnc)
        if p:
            sys.exit("refused: " + "; ".join(p))
        hit.update(updates)
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            w.writerows(existing)
        print(f"updated {hit['name']} @ {hit['company']}: {', '.join(updates)}")
        return 0

    if len(argv) != 1:
        print(__doc__)
        return 2
    with open(argv[0], encoding="utf-8") as f:
        new_rows = json.load(f)
    if not isinstance(new_rows, list):
        sys.exit("rows file must hold a JSON list of objects")
    keys = {(r["job_link"].strip(), r["name"].strip().lower()) for r in existing}
    accepted, refused = [], []
    for r in new_rows:
        row = {c: str(r.get(c, "") or "") for c in COLUMNS}
        row["status"] = row["status"] or "new"
        row["confidence"] = row["confidence"].lower()
        p = problems(row, keys, dnc)
        if p:
            refused.append((row["name"], p))
            continue
        keys.add((row["job_link"].strip(), row["name"].strip().lower()))
        accepted.append(row)
    write_header = not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if write_header:
            w.writeheader()
        for row in accepted:
            w.writerow(row)
    print(f"appended {len(accepted)}, refused {len(refused)}")
    for name, p in refused:
        print(f"refused {name}: " + "; ".join(p))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
