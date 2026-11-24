#!/usr/bin/env python3
"""Dedup store for postings the automation has seen: stopgap-private/seen-links.txt.

Usage:
  seen_links.py check <link> [<company> <title>]   -> prints "seen" or "new"; exit 0 seen, 1 new
  seen_links.py add <link> <company> <title> <status> [<source>]   -> appends one line (no duplicates)
  seen_links.py bulk <jsonl file>   -> each line {"link","company","title","status","source"}; appends the new ones
  seen_links.py stats

Line format: <link> | <company> | <title> | <status> | <YYYY-MM-DD> | <source>
A posting is "seen" when its link matches (query part and trailing slash ignored) or when the
normalized company+title matches. Nothing is ever rewritten or removed by this script.
"""
import datetime
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from runtime_paths import PRIVATE
PATH = os.path.join(PRIVATE, "seen-links.txt")


def norm_link(u):
    if not (u or '').strip():
        return ''
    sys.path.insert(0, ROOT)
    from job_runtime.policy import canonical_url
    return canonical_url(u)


def norm_ct(company, title):
    f = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    return f(company) + "|" + f(title)


def load():
    links, cts = set(), set()
    if os.path.exists(PATH):
        for line in open(PATH, encoding="utf-8", errors="ignore"):
            parts = [p.strip() for p in line.rstrip("\n").split("|")]
            if len(parts) >= 3:
                if parts[0].startswith("http"):
                    links.add(norm_link(parts[0]))
                cts.add(norm_ct(parts[1], parts[2]))
    return links, cts


def seen(links, cts, link, company="", title=""):
    if link and norm_link(link) in links:
        return True
    if not link and company and title and norm_ct(company, title) in cts:
        return True
    return False


def add_lines(rows):
    import fcntl
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(PATH + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _add_lines(rows)


def _add_lines(rows):
    links, cts = load()
    today = datetime.datetime.now(__import__('zoneinfo').ZoneInfo('America/New_York')).date().isoformat()
    added = 0
    with open(PATH, "a", encoding="utf-8") as f:
        for r in rows:
            link, company, title = r.get("link", ""), r.get("company", ""), r.get("title", "")
            if seen(links, cts, link, company, title):
                continue
            f.write(" | ".join([link, company, title, r.get("status", ""), today, r.get("source", "")]) + "\n")
            links.add(norm_link(link))
            cts.add(norm_ct(company, title))
            added += 1
        f.flush()
        os.fsync(f.fileno())
    return added


def main(a):
    if not a:
        print(__doc__)
        return 2
    cmd = a[0]
    if cmd == "check":
        links, cts = load()
        s = seen(links, cts, a[1], a[2] if len(a) > 2 else "", a[3] if len(a) > 3 else "")
        print("seen" if s else "new")
        return 0 if s else 1
    if cmd == "add":
        n = add_lines([{"link": a[1], "company": a[2], "title": a[3], "status": a[4], "source": a[5] if len(a) > 5 else ""}])
        print(f"added {n}")
        return 0
    if cmd == "bulk":
        rows = [json.loads(l) for l in open(a[1], encoding="utf-8") if l.strip()]
        print(f"added {add_lines(rows)} of {len(rows)}")
        return 0
    if cmd == "stats":
        links, cts = load()
        print(f"{len(links)} links, {len(cts)} company+title keys")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
