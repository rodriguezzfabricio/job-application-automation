#!/usr/bin/env python3
"""One-time migration of the Claude Doc Jobs table into tracker.sqlite (2026-11-04 switch to the Sheet).

Usage: migrate_doc.py READ_FILE [READ_FILE2 ...] --out migrate.json [--apply]
READ_FILE is a saved result of the docs read tool (the JSON array with a "text" item holding
{"verdict":"allow","data":{"xml": ...}}), one per page of the table view. --apply runs tracker.py upsert-job
on the rows; without it the rows are only written to --out for inspection.
Status mapping: 0 New, 1 Matched, 2 Ready to fill, 3 Awaiting submit (or Walled when the Notes say
"opened for you" or "Walled site"), 4 Applied, 5 Rejected by me, 6 Not eligible.
"""
import json
import os
import re
import subprocess
import sys
from xml.etree import ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ST = {"0": "New", "1": "Matched", "2": "Ready to fill", "3": "Awaiting submit", "4": "Applied", "5": "Rejected by me", "6": "Not eligible"}


def load(path):
    d = json.load(open(path, encoding="utf-8"))
    if isinstance(d, list):
        d = [json.loads(x["text"]) for x in d if isinstance(x, dict) and x.get("text", "").startswith("{")][0]
    return d


def rows_of(d):
    root = ET.fromstring(d["data"]["xml"])
    tb = root.find("table")
    out = []
    for r in tb.findall("row"):
        cells = []
        for c in r.findall("cell"):
            parts, href, drop = [], None, None
            for el in c.iter():
                if el.tag == "link":
                    href = el.get("href")
                if el.tag == "dropdown":
                    drop = el.get("index")
                if el.text and el.tag in ("text", "link"):
                    parts.append(el.text)
                if el.tail and el.tag == "link":
                    parts.append(el.tail)
            cells.append({"t": "".join(parts).strip(), "href": href, "drop": drop})
        out.append((r.get("id"), cells))
    return out


def main(argv):
    files = [a for a in argv if not a.startswith("--") and a != argv[argv.index("--out") + 1]] if "--out" in argv else []
    out_path = argv[argv.index("--out") + 1]
    apply = "--apply" in argv
    seen, allrows = set(), []
    for f in files:
        for rid, cells in rows_of(load(f)):
            if rid not in seen:
                seen.add(rid)
                allrows.append(cells)
    jobs = []
    for c in allrows[1:]:
        if len(c) < 11:
            continue
        g = lambda i: c[i]["t"]
        notes, elig = g(10), g(5)
        status = ST.get(c[6]["drop"], "New")
        if status == "Awaiting submit" and re.search(r"opened for you|walled site", notes, re.I):
            status = "Walled"
        m = re.match(r"(\d+)", elig)
        src = re.search(r"source:\s*([^;]+)", notes)
        jobs.append(dict(company=g(1), title=g(2), location=g(3), link=c[4]["href"] or g(4), score=int(m.group(1)) if m else None, eligibility=elig,
                         status=status, resume="" if g(7) in ("—", "-", "") else g(7), found_at=g(0), source=src.group(1).strip() if src else "",
                         notes=notes, reason=elig if status == "Not eligible" else ""))
    jobs = [j for j in jobs if j["company"] and j["title"] and j["company"] != "Example Co."]
    json.dump(jobs, open(out_path, "w", encoding="utf-8"), indent=0)
    print(json.dumps({"rows_read": len(allrows) - 1, "jobs": len(jobs), "by_status": {s: sum(1 for j in jobs if j["status"] == s) for s in set(j["status"] for j in jobs)}}))
    if apply:
        p = subprocess.run([sys.executable, "-I", os.path.join(HERE, "tracker.py"), "upsert-job", out_path], capture_output=True, text=True)
        print(p.stdout[:300], p.stderr[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
