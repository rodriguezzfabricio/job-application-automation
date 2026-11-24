#!/usr/bin/env python3
"""Decides, from the tracker and the settings, what this run matches, fills, opens and researches (2026-11-04).
The coordinator passes the plan to the stages; no stage re-decides these rules.

Usage: plan.py --hour HH --settings FILE.json --state FILE.json [--walled "Example Studio,Example Media"] [--hold "Example Labs"]
settings: fill_mode (hourly|windows), fill_windows ("08,12,17"), max_fills_per_day, max_posting_age_hours, paused (yes|no),
          linkedin_loads_per_day (default 60), recruiter_companies_per_run (default 2)
state:    last_full_fill_at (ISO UTC) from desktop-state.json
Prints JSON: to_match, to_fill (ordered), walled_to_open, fill_full, fills_left_today, recruiter_loads, recruiter_companies,
             skipped {job_key: reason}, counts.
Rules (owner): 3 fills per FULL run and 2 per FAST run (--mode, 2026-11-12), newest posting first, 25 per Eastern day, 2 per company per day unless Ready to fill; Ready to fill first, then
Matched by score (70+ before 50-69); never fill a row whose company+title or link is Applied; never a PM row unless Ready to
fill; never a weak match, "needs you" or "no description" row unless Ready to fill; posted within max_posting_age_hours
unless Ready to fill; walled companies are opened (2 per company per day, 2 ever for a configured group, only 85+ scores, only submits count) not filled;
companies on hold get nothing.
One click: rows whose notes say
"easy apply only" / "quick apply only" go to to_one_click for stages/one-click.md, not to to_fill. Settings
one_click_per_day (default 10), one_click_per_run (3), one_click_max_age_hours (72, the Today tab's lifetime). Newest
posting first. Same gates as a fill (applied, hold, repost, closed, weak match, score 50, 2 per company per day), and a
resume must be picked first (such rows go to to_match). A row whose notes say "one-click sending <date>" with no result is
his to resolve, never sent again.
Signed-in sites (2026-11-10 step 3; he makes the account or signs in with Brave's password manager, the agent never
does): a job on a site whose portals.py state is "ready" (he set Mark "signed up", the agent got in since the last wall,
or he has an application page of that site open) is filled there instead of opened for him. A Walled row on such a site
counts as his go-ahead, like "fill now", but keeps the repost/closed checks and a 72 h age limit.
"""
import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portals as P  # noqa: E402  (2026-11-10 step 3: sites where he is signed in)
from titles import title_key  # noqa: E402  (2026-11-11: Jr = Junior when checking for an application already made)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from runtime_paths import PRIVATE
DB_PATH = os.environ.get("TRACKER_DB") or os.path.join(PRIVATE, "tracker.sqlite")
ET = ZoneInfo("America/New_York")
# which blocked the next two 20-minute runs. FULL now does fills_per_run (3), FAST runs fast_fills_per_run (2).
RUN_LIMIT = 3
FAST_RUN_LIMIT = 2


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def company_key(name):
    name = re.sub(r"\(.*?\)", " ", name or "")
    name = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|via .*)\b\.?", " ", name, flags=re.I)
    return norm(name)


def parse_dt(s):
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            d = datetime.datetime.strptime(s.replace("Z", "+0000") if s.endswith("Z") and "%z" in fmt else s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=ET)
        except ValueError:
            continue
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return datetime.datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=ET) if m else None


def posted_time(job):
    """When the posting went up. A date with no time counts from the end of that day."""
    raw = str(job["posted_at"] or "").strip()
    d = parse_dt(raw) or parse_dt(job["found_at"])
    if d and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        d = d.replace(hour=23, minute=59)
    return d



# where a job's form lives (2026-11-05): one-page ATS forms first, account portals are opened for him, aggregator links
# (LinkedIn, Handshake, Indeed, Wellfound) are resolved to the employer page first
EASY_ATS = ("greenhouse.io", "grnh.se", "jobs.dover.com", "lever.co", "ashbyhq.com", "smartrecruiters.com", "rippling.com", "bamboohr.com", "workable.com", "jobvite.com", "breezy.hr", "recruitee.com")
ACCOUNT_PORTALS = ("myworkdayjobs.com", "icims.com", "taleo.net", "successfactors", "oraclecloud.com", "brassring.com", "ultipro.com", "paylocity.com", "amazon.jobs")
AGGREGATORS = ("linkedin.com", "joinhandshake.com", "indeed.com", "wellfound.com", "builtin.com", "ziprecruiter.com")


DEAD = (
    (r"no longer accepting applications|not currently accepting applications|posting removed|job id invalid|posting closed|job unavailable|job not found", "posting closed: nothing to fill"),
    (r"reposted .{0,40}do not apply|repost rule", "repost: never applied to"),
    (r"application submitted|already applied", "his own application already shows on the site"),
    (r"fill skipped \d{4}-\d{2}-\d{2}", "a fill was tried and skipped before: see the row's notes"),
    (r"employer link not found|no employer form|generic meeboss|no specific job|aggregator page", "no employer form to fill"),
)


MIN_SCORE = 35
ONE_CLICK = re.compile(r"easy apply only|quick apply only|1-click apply only", re.I)
# a one-click row has no employer form by nature, so "employer link not found" is not dead for it
DEAD_ONE_CLICK = DEAD[:3] + ((r"one-click skipped \d{4}-\d{2}-\d{2}", "one-click was tried and skipped before: see the row's notes"),)


def dead_reason(notes, one_click=False):
    for pat, why in (DEAD_ONE_CLICK if one_click else DEAD):
        if re.search(pat, notes or "", re.I):
            return why
    return ""


def site_class(link):
    l = (link or "").lower()
    if any(x in l for x in EASY_ATS):
        return 0
    if any(x in l for x in ACCOUNT_PORTALS):
        return 3
    if any(x in l for x in AGGREGATORS):
        return 2
    return 1

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hour", required=True)
    ap.add_argument("--settings", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--walled", default="")
    ap.add_argument("--hold", default="")
    ap.add_argument("--mode", default="FULL", choices=["FULL", "FAST"])
    a = ap.parse_args()
    st = json.load(open(a.settings, encoding="utf-8"))
    state = json.load(open(a.state, encoding="utf-8")) if os.path.exists(a.state) else {}
    now = datetime.datetime.now(ET)
    today = now.strftime("%Y-%m-%d")
    hour = int(a.hour)
    windows = [int(x) for x in re.findall(r"\d+", str(st.get("fill_windows", "08,12,17")))]
    fill_mode = str(st.get("fill_mode", "windows")).lower()
    paused = str(st.get("paused", "no")).lower() == "yes"
    max_day = int(st.get("max_fills_per_day", 25))
    max_age = float(st.get("max_posting_age_hours", 24))
    loads_cap = int(st.get("linkedin_loads_per_day", 60))
    rec_companies = int(st.get("recruiter_companies_per_run", 2))
    oc_day = int(st.get("one_click_per_day", 10))
    oc_run = int(st.get("one_click_per_run", 3))
    oc_age = float(st.get("one_click_max_age_hours", 72))
    run_limit = int(st.get("fast_fills_per_run", FAST_RUN_LIMIT)) if a.mode == "FAST" else int(st.get("fills_per_run", RUN_LIMIT))
    resume_only = str(st.get("resume_only", "") or "").strip()
    _lib = os.path.join(os.path.dirname(DB_PATH), "my-resumes", "manifest.json")
    library = {r["file"] for r in json.load(open(_lib, encoding="utf-8"))["resumes"]} if os.path.exists(_lib) else set()
    if resume_only and library and resume_only not in library:
        resume_only = ""
    last_full = parse_dt(state.get("last_full_fill_at"))
    passed = [w for w in windows if w <= hour]
    catch_up = bool(passed) and (last_full is None or last_full.astimezone(ET).strftime("%Y-%m-%d %H") < f"{today} {passed[-1]:02d}")
    fill_full = (not paused) and (fill_mode == "hourly" or hour in windows or catch_up)

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    jobs = [dict(r) for r in con.execute("SELECT * FROM jobs")]
    apps = [dict(r) for r in con.execute("SELECT * FROM applications")]
    applied_keys = {(j["company_key"], title_key(j["title"])) for j in jobs if j["status"] == "Applied"} | {(x["company_key"], title_key(x["title"])) for x in apps}
    applied_links = {j["job_key"] for j in jobs if j["status"] == "Applied"} | {x["job_key"] for x in apps if x["job_key"]}
    # 2026-11-09: he applied to Planview himself; the email gave no title, so the open Planview row stayed fillable
    unknown_recent = {}
    for x in apps:
        d = parse_dt(x["applied_at"])
        if norm(x["title"]) == "" and d and (now - d.astimezone(ET)).days <= 3:
            unknown_recent[x["company_key"]] = x["applied_at"][:10]
    today_fills = [j for j in jobs if re.search(r"(filled|submitted|submitting|opened for you) " + today, j["notes"] or "")]
    today_task_apps = [x for x in apps if x["applied_at"].startswith(today) and x["submitted_by"] == "task"]
    fills_today = len({j["job_key"] for j in today_fills} | {x["job_key"] for x in today_task_apps if x["job_key"]})
    per_company_today = {}
    for j in today_fills:
        per_company_today[j["company_key"]] = per_company_today.get(j["company_key"], 0) + 1
    # stopped or skipped were using up the cap, so 8 "tries" left 1 real application)
    oc_today_re = re.compile(r"one-click applied " + today)
    oc_keys = {j["job_key"] for j in jobs if oc_today_re.search(j["notes"] or "")}
    oc_keys |= {x["job_key"] for x in apps if x["applied_at"].startswith(today) and x["submitted_by"] == "task"
                and re.search(r"easy apply|quick apply", x["confirmed_via"] or "", re.I) and x["job_key"]}
    # rows touched today per company (fills, opened pages, one-click tries), each row once
    touched = {j["job_key"]: j["company_key"] for j in today_fills}
    touched.update({j["job_key"]: j["company_key"] for j in jobs if j["job_key"] in oc_keys})
    walled_names = {company_key(x) for x in a.walled.split(",") if x.strip()}
    # a Walled row that is only LinkedIn Easy Apply is not a wall: it goes to the one-click queue
    walled_names |= {j["company_key"] for j in jobs if (j["status"] == "Walled" and not ONE_CLICK.search(j["notes"] or "")) or "needs you: account required" in (j["notes"] or "")}
    hold = {company_key(x) for x in a.hold.split(",") if x.strip()}
    limited_companies = {company_key(c.strip()) for c in a.walled.split(",") if c.strip()}
    limited_submissions = sum(1 for j in jobs if j["company_key"] in limited_companies and j["status"] == "Applied")
    LIMITED_MIN_SCORE = 85

    try:
        prow = {r["portal"]: dict(r) for r in con.execute("SELECT * FROM portals")}
    except sqlite3.OperationalError:
        prow = {}
    tabs_open = set()
    try:
        snap = json.load(open(os.path.join(os.path.dirname(DB_PATH), "scratch", "open-tabs.json"), encoding="utf-8"))
        at = parse_dt(snap.get("at"))
        if at and (now - at).total_seconds() <= 2 * 3600:
            tabs_open = set(snap.get("portal_tabs", []))
    except Exception:
        pass

    def pstate(link):
        p = P.portal_of(link)
        return P.state(prow.get(p), p in tabs_open, now.replace(tzinfo=None))

    skipped, to_match, cands, walled_open, oc_cands = {}, [], [], [], []
    go_keys, signed_keys = set(), set()
    for j in jobs:
        key, n = j["job_key"], (j["notes"] or "")
        is_ready = j["status"] == "Ready to fill"
        signed_in = pstate(j["link"]) == "ready"
        # a Walled row on a site where he is now signed in: his go-ahead (not a CAPTCHA wall, not a filled form)
        portal_go = j["status"] == "Walled" and signed_in and not ONE_CLICK.search(n) and not re.search(r"captcha|filled \d{4}-\d{2}-\d{2}", n, re.I)
        go = is_ready or portal_go
        if signed_in:
            signed_keys.add(key)
        if go:
            go_keys.add(key)
        if j["status"] not in ("New", "Matched", "Ready to fill") and not (j["status"] == "Walled" and ONE_CLICK.search(n)) and not portal_go:
            continue
        if j["company_key"] in hold:
            skipped[key] = "company on hold"
            continue
        if (j["company_key"], title_key(j["title"])) in applied_keys or key in applied_links:
            skipped[key] = "already applied (same company and title)"
            continue
        if j["company_key"] in unknown_recent and not is_ready:
            skipped[key] = f"an application to this company was confirmed on {unknown_recent[j['company_key']]} (email gave no title): check before applying again"
            continue
        score = j["score"] if j["score"] is not None else -1
        resume_ok = bool(resume_only) or (bool(j["resume"]) and j["resume"].endswith(".pdf") and (not library or j["resume"] in library))
        if ONE_CLICK.search(n) and j["company_key"] not in limited_companies:
            why = "" if is_ready else dead_reason(n, one_click=True)
            posted = posted_time(j)
            if why:
                skipped[key] = why
            elif re.search(r"one-click sending \d{4}-\d{2}-\d{2}", n) and not re.search(r"one-click applied|submit result unknown", n):
                skipped[key] = "an Easy Apply / quick apply may have gone through: his to resolve"
            elif not is_ready and posted and (now - posted.astimezone(ET)).total_seconds() > oc_age * 3600:
                skipped[key] = f"one-click: older than {int(oc_age)} h (posted {j['posted_at'] or j['found_at']})"
            elif not is_ready and re.search(r"PM role: track only|match refused|no description(?!; match stage will read)", n):
                skipped[key] = "one-click: PM or no description (set fill now to apply anyway)"
            elif not is_ready and "needs you" in re.sub(r"needs you: (unverified requirements|account required)", "", n):
                skipped[key] = "one-click: needs you (see notes)"
            elif not is_ready and score < MIN_SCORE:
                skipped[key] = f"score under {MIN_SCORE}"
            elif not resume_ok:
                to_match.append(j)
                skipped[key] = "one-click: waiting for MATCH to pick a resume"
            elif not fill_full and not is_ready:
                skipped[key] = "one-click runs in fill runs"
            else:
                oc_cands.append(j)
            continue
        if j["status"] == "Walled" and not portal_go:
            continue
        if j["company_key"] in walled_names and not go and not signed_in:
            if j["status"] in ("New", "Matched") and "opened for you" not in n and score >= MIN_SCORE:
                walled_open.append(j)
            else:
                skipped[key] = "walled company"
            continue
        # matching
        dead = "" if is_ready else dead_reason(n)
        if dead:
            skipped[key] = dead
            continue
        posted = posted_time(j)
        age_h = (now - posted.astimezone(ET)).total_seconds() / 3600 if posted else 0
        if portal_go and age_h > oc_age:
            skipped[key] = f"you are signed in on its site, but it is older than {int(oc_age)} h (posted {j['posted_at'] or j['found_at']})"
            continue
        if not go and age_h > max_age:
            skipped[key] = f"older than {int(max_age)} h (posted {j['posted_at'] or j['found_at']})"
            continue
        if not resume_ok and (go or (score >= MIN_SCORE and j["status"] in ("New", "Matched"))) and not re.search(r"no description(?!; match stage will read)|match refused|weak match", n):
            to_match.append(j)
        # filling
        if not fill_full and not go:
            continue
        if not go:
            if not resume_ok or score < MIN_SCORE:
                skipped[key] = "no resume yet" if not resume_ok else f"score under {MIN_SCORE}"
                continue
            if re.search(r"needs you|no description(?!; match stage will read)|PM role: track only|match refused", n.replace("needs you: unverified requirements", "")):
                skipped[key] = "needs you, PM or no description"
                continue
            posted = posted_time(j)
            if posted and (now - posted.astimezone(ET)).total_seconds() > max_age * 3600:
                skipped[key] = f"older than {int(max_age)} h (posted {j['posted_at'] or j['found_at']})"
                continue
            if per_company_today.get(j["company_key"], 0) >= 2:
                skipped[key] = "company already has 2 fills today"
                continue
        if re.search(r"submitting \d{4}-\d{2}-\d{2}", n) and not re.search(r"submitted|submit result unknown", n):
            skipped[key] = "a submit may have gone through: his to resolve"
            continue
        if site_class(j["link"]) == 3 and not go and not signed_in:
            # account portal (Workday and the like): the task cannot sign up, so it is opened for him instead of tried
            # (once he signs in there, portals.py says "ready" and it is filled like any form: step 3)
            if "opened for you" not in n:
                walled_open.append(j)
            else:
                skipped[key] = "account portal, already opened for you"
            continue
        cands.append(j)
    # his go-ahead first ("fill now", or he signed in on the site: his session may not last), then the NEWEST posting
    # 1-minute-old 75), then score, then one-page forms
    cands.sort(key=lambda j: (0 if j["job_key"] in go_keys else 1, 0 if j["job_key"] in signed_keys else 1, -(posted_time(j) or now).timestamp(), -(j["score"] or 0), site_class(j["link"]), j["found_at"]))
    to_match.sort(key=lambda j: (0 if j["job_key"] in go_keys else 1, site_class(j["link"]), -(j["score"] or 0), j["found_at"]))
    # one fill per employer link: two LinkedIn posts can lead to the same employer form
    _seen_links, _uniq = set(), []
    for j in cands:
        lk = (j["link"] or "").split("?")[0].rstrip("/").lower()
        if lk and lk in _seen_links:
            skipped[j["job_key"]] = "same employer form as another row"
            continue
        _seen_links.add(lk)
        _uniq.append(j)
    cands = _uniq
    left = max(0, max_day - fills_today)
    to_fill, per_run = [], {}
    for j in cands:
        if len(to_fill) >= min(run_limit, left):
            skipped[j["job_key"]] = "over the per-run or per-day limit, next run"
            continue
        c = j["company_key"]
        if j["job_key"] not in go_keys and per_company_today.get(c, 0) + per_run.get(c, 0) >= 2:
            skipped[j["job_key"]] = "company already has 2 fills today"
            continue
        per_run[c] = per_run.get(c, 0) + 1
        to_fill.append(j)
    # one click: his own "fill now" first, then the newest posting (more people apply to it every hour), then score
    oc_cands.sort(key=lambda j: (0 if j["status"] == "Ready to fill" else 1, -(posted_time(j) or now).timestamp(), -(j["score"] or 0)))
    oc_left = max(0, oc_day - len(oc_keys))
    to_one_click, oc_seen = [], set()
    for j in oc_cands:
        c, ct = j["company_key"], (j["company_key"], title_key(j["title"]))
        if ct in oc_seen:
            skipped[j["job_key"]] = "same company and title as another one-click row"
            continue
        if len(to_one_click) >= min(oc_run, oc_left):
            skipped[j["job_key"]] = f"one-click: over the {oc_run}-per-run or {oc_day}-a-day limit, next run"
            continue
        done_today = sum(1 for k, v in touched.items() if v == c and k != j["job_key"])
        if j["status"] != "Ready to fill" and done_today + per_run.get(c, 0) >= 2:
            skipped[j["job_key"]] = "company already has 2 applications today"
            continue
        per_run[c] = per_run.get(c, 0) + 1
        oc_seen.add(ct)
        to_one_click.append(j)
    walled_open.sort(key=lambda j: -(j["score"] or 0))
    chosen_walled, per_w = [], {}
    for j in walled_open:
        c = j["company_key"]
        if c in limited_companies and (j["score"] or 0) < LIMITED_MIN_SCORE:
            skipped[j["job_key"]] = f"Limited-company policy: the 2 slots are saved for jobs scoring {LIMITED_MIN_SCORE}+"
            continue
        if c in limited_companies and limited_submissions + len([x for x in chosen_walled if x["company_key"] in limited_companies]) >= 2:
            skipped[j["job_key"]] = "Limited-company policy allow 2 applications ever"
            continue
        if per_company_today.get(c, 0) + per_w.get(c, 0) >= 2:
            skipped[j["job_key"]] = "company already has 2 opened today"
            continue
        if not fill_full:
            skipped[j["job_key"]] = "walled rows are opened in fill runs"
            continue
        per_w[c] = per_w.get(c, 0) + 1
        chosen_walled.append(j)
    loads_today = con.execute("SELECT value FROM counters WHERE day=? AND name='linkedin_loads'", (today,)).fetchone()
    loads_today = loads_today[0] if loads_today else 0
    rec_loads = max(0, min(10, loads_cap - loads_today))
    todo_companies = con.execute("SELECT COUNT(*) FROM companies c WHERE research_status='todo' AND EXISTS (SELECT 1 FROM applications a WHERE a.company_key=c.company_key AND a.result<>'rejected')").fetchone()[0]
    out = {
        "hour": hour, "mode": a.mode, "fills_per_run": run_limit, "fill_full": fill_full, "paused": paused, "catch_up": catch_up, "fills_today": fills_today, "fills_left_today": left,
        "to_match": [j["job_key"] for j in to_match[:25]],
        "to_fill": [{"job_key": j["job_key"], "company": j["company"], "title": j["title"], "status": j["status"], "score": j["score"], "resume": resume_only or j["resume"],
                     "portal": P.portal_of(j["link"]), "signed_in": j["job_key"] in signed_keys} for j in to_fill],
        "signed_in_sites": sorted(p for p in set(prow) | tabs_open if P.state(prow.get(p), p in tabs_open, now.replace(tzinfo=None)) == "ready"),
        "to_resolve": [{"job_key": j["job_key"], "company": j["company"], "link": j["link"]} for j in cands if j not in to_fill and re.search(r"linkedin\.com/jobs/view|joinhandshake\.com/jobs/\d", j["link"] or "") and not re.search(r"employer link not found|easy apply only|quick apply only", j["notes"] or "", re.I)][:15],
        "to_one_click": [{"job_key": j["job_key"], "company": j["company"], "title": j["title"], "link": j["link"], "site": "handshake" if "joinhandshake.com" in (j["link"] or "") else "linkedin",
                          "status": j["status"], "score": j["score"], "resume": resume_only or j["resume"]} for j in to_one_click],
        "one_click_today": len(oc_keys), "one_click_left_today": oc_left,
        "walled_to_open": [{"job_key": j["job_key"], "company": j["company"], "title": j["title"], "link": j["link"]} for j in chosen_walled],
        "recruiter_loads": rec_loads if todo_companies else 0, "recruiter_companies": min(rec_companies, todo_companies), "linkedin_loads_today": loads_today,
        "skipped": skipped,
        "counts": {"jobs": len(jobs), "open": sum(1 for j in jobs if j["status"] in ("New", "Matched", "Ready to fill", "Awaiting submit", "Walled")), "to_match": len(to_match), "to_fill": len(to_fill), "to_one_click": len(to_one_click), "walled": len(chosen_walled), "companies_todo": todo_companies},
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
