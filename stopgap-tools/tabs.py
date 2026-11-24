#!/usr/bin/env python3
"""Manage one browser tab per opening.

Usage (python3 -I tabs.py ...):
  list                 print every open Brave tab URL (all windows, the task's tab groups included)
  has <url>            exit 0 when a tab with the same job URL is open, 1 when not
  open <url>           open the URL in his Brave only when no tab with the same job URL is open; prints "opened" or "already open"
  close-older <url>    keep the newest tab of this job (highest tab id), close the older copies; prints how many
  sweep [--dry-run]    end-of-run cleanup (2026-11-10, owner: "I see all these tabs open but I don't know what applied"):
                       1. jobs that needed him but are older than 72 h become Stale (they leave the Today tab)
                       2. tabs of jobs that are finished (Applied, Not eligible, Rejected by me, Stale) are closed
                       3. a filled form whose tab is gone is queued once to be filled again (Ready to fill)
                       4. later copies of a job tab are closed; scratch/open-tabs.json records which jobs have a tab
                       Tabs that match no job are never touched. Prints JSON.
  snapshot             start of a run: write scratch/open-tabs.json (jobs with a tab, and "portal_tabs": sites where an
                       application page is open, so he is signed in there) without closing anything; prints JSON
  dedupe               close later copies of a job URL, keeping the first tab (the first one is the filled form when a
                       stage filled a tab and then opened a copy); prints how many it closed
"Same job URL" = scheme and www dropped, query and fragment dropped except job-id parameters (gh_jid, jk, currentJobId,
token), trailing slash and a trailing /application or /apply dropped, lower case. Blank tabs (newtab) are ignored.
"""
import json
import os
import re
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

KEEP_PARAMS = ("gh_jid", "jk", "currentjobid", "token", "for")


def norm(u):
    u = (u or "").strip()
    if not u or u.startswith(("chrome://", "brave://", "about:")):
        return ""
    p = urlsplit(u)
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    path = re.sub(r"/(application|apply)/?$", "", p.path.rstrip("/").lower())
    q = {k.lower(): v for k, v in parse_qs(p.query).items()}
    keep = "&".join(f"{k}={q[k][0]}" for k in KEEP_PARAMS if k in q)
    return host + path + ("?" + keep if keep else "")


def osa(script):
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30).stdout


def tabs():
    out = osa('set o to ""\ntell application "Brave Browser"\nrepeat with w in windows\nrepeat with t in tabs of w\n'
              'set o to o & (id of w) & "\t" & (id of t) & "\t" & (URL of t) & linefeed\nend repeat\nend repeat\nend tell\nreturn o')
    rows = []
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) == 3:
            rows.append((p[0], p[1], p[2]))
    return rows


def sweep(dry=False):
    import datetime
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tracker as T
    con = T.db()
    now = datetime.datetime.now(T.ET)
    jobs = {j["job_key"]: j for j in con.execute("SELECT * FROM jobs")}
    index = {}
    for k, j in jobs.items():
        for u in (j["link"], T.form_url(j["notes"])):
            n = norm(u)
            if n:
                index.setdefault(n, set()).add(k)
    out = {"expired": [], "closed": [], "requeued": [], "dry_run": dry}
    # 1. retire what was never done in time
    for k, j in jobs.items():
        if T.today_section(j) and T.is_expired(j, now):
            out["expired"].append(j["company"])
            if not dry:
                T.set_job(con, k, {"status": "Stale", "reason": f"not done within {T.TODAY_HOURS} h of posting; dropped from Today {now:%Y-%m-%d %H:%M}"})
    if not dry:
        con.commit()
    jobs = {j["job_key"]: j for j in con.execute("SELECT * FROM jobs")}
    # 2. close tabs of finished jobs
    rows, open_keys = tabs(), set()
    for w, t, u in rows:
        keys = index.get(norm(u), set())
        if not keys:
            continue
        if all(jobs[k]["status"] in T.FINAL for k in keys):
            out["closed"].append(jobs[sorted(keys)[0]]["company"])
            if not dry:
                osa(f'tell application "Brave Browser" to close (tabs of (window id {w}) whose id is {t})')
        else:
            open_keys |= keys
    # 3. a filled form lost its tab: fill it again, once
    for k, j in jobs.items():
        n = j["notes"] or ""
        if j["status"] != "Awaiting submit" or k in open_keys or T.is_expired(j, now):
            continue
        if re.search(r"submitting \d|submit result unknown|refilled once", n):
            continue
        out["requeued"].append(j["company"])
        if not dry:
            T.set_job(con, k, {"status": "Ready to fill", "notes": f"form tab gone {now:%Y-%m-%d %H:%M}: refilled once; {n}"[:1900]})
    if not dry:
        con.commit()
        os.makedirs(os.path.dirname(T.OPEN_TABS_PATH), exist_ok=True)
        json.dump({"at": f"{now:%Y-%m-%d %H:%M}", "open_job_keys": sorted(open_keys), "portal_tabs": portal_tabs(rows)}, open(T.OPEN_TABS_PATH, "w", encoding="utf-8"))
    con.close()
    # 4. duplicates
    out["duplicates_closed"] = 0 if dry else dedupe()
    for x in ("expired", "closed", "requeued"):
        out[x + "_count"] = len(out[x])
    print(json.dumps(out))
    return 0


def portal_tabs(rows):
    """Sites (portals.py) where an application page is open: he is signed in there, or about to be (step 3)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import portals as P
    return sorted({P.portal_of(u) for w, t, u in rows if P.is_apply_page(u)} - {""})


def snapshot():
    """Start of a run (2026-11-10 step 3): record which jobs have a tab and which sites have an application page open,
    closing nothing. plan.py reads it to let the agent fill on a site where he just signed in."""
    import datetime
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tracker as T
    con = T.db()
    index = {}
    for j in con.execute("SELECT job_key, link, notes FROM jobs WHERE status NOT IN ('Applied','Not eligible','Rejected by me','Stale')"):
        for u in (j["link"], T.form_url(j["notes"])):
            n = norm(u)
            if n:
                index.setdefault(n, set()).add(j["job_key"])
    con.close()
    rows = tabs()
    open_keys = set()
    for w, t, u in rows:
        open_keys |= index.get(norm(u), set())
    data = {"at": datetime.datetime.now(T.ET).strftime("%Y-%m-%d %H:%M"), "open_job_keys": sorted(open_keys), "portal_tabs": portal_tabs(rows)}
    os.makedirs(os.path.dirname(T.OPEN_TABS_PATH), exist_ok=True)
    json.dump(data, open(T.OPEN_TABS_PATH, "w", encoding="utf-8"))
    print(json.dumps(data))
    return 0


def dedupe():
    seen, closed = set(), 0
    for w, t, u in tabs():
        n = norm(u)
        if not n:
            continue
        if n in seen:
            osa(f'tell application "Brave Browser" to close (tabs of (window id {w}) whose id is {t})')
            closed += 1
        else:
            seen.add(n)
    return closed


def main(a):
    if not a:
        print(__doc__)
        return 2
    cmd = a[0]
    if cmd == "list":
        for w, t, u in tabs():
            print(u)
        return 0
    if cmd in ("has", "open"):
        n = norm(a[1])
        found = any(norm(u) == n for _, _, u in tabs()) if n else False
        if cmd == "has":
            return 0 if found else 1
        if found:
            print("already open")
            return 0
        subprocess.run(["open", "-a", "Brave Browser", a[1]])
        print("opened")
        return 0
    if cmd == "close-older":
        # keep the newest tab of this job (the highest tab id: the form the task just filled), close the older copies
        n = norm(a[1])
        same = sorted(((int(t), w) for w, t, u in tabs() if n and norm(u) == n and t.isdigit()), reverse=True)
        for t, w in same[1:]:
            osa(f'tell application "Brave Browser" to close (tabs of (window id {w}) whose id is {t})')
        print(f"closed {max(0, len(same) - 1)}")
        return 0
    if cmd == "sweep":
        return sweep("--dry-run" in a)
    if cmd == "snapshot":
        return snapshot()
    if cmd == "dedupe":
        print(f"closed {dedupe()}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
