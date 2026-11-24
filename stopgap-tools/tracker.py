#!/usr/bin/env python3
"""The job tracker database behind the Job Dashboard sheet (2026-11-04 redesign).

The Google Sheet "Job Dashboard" is what the owner reads and edits. This SQLite file is what the
automation reads and writes; every run mirrors it into the sheet (export) and reads his edits back
(import-edits). Nothing here talks to Google: the hourly task moves the JSON this prints through the
Sheets connector tools.

Usage (every command prints JSON; data files are under stopgap-private/):
  tracker.py init
  tracker.py upsert-job FILE.json            one object or a list: company, title, link, [location, score,
                                             eligibility, status, resume, posted_at, found_at, source, notes, reason]
                                             -> prints {"added": n, "updated": n, "keys": [...]}
  tracker.py set-job JOB_KEY field=value ... (status, resume, notes, score, posted_at, reason, location, title)
  tracker.py get-job JOB_KEY
  tracker.py find-job --company C [--title T] [--link L]
  tracker.py list-jobs [--status S ...] [--company C] [--min-score N] [--limit N]
  tracker.py add-application FILE.json      one object or a list: company, [title, link, applied_at, submitted_by,
                                             confirmed_via, result, result_at, source_id]; links to a job by link,
                                             then by company+title; otherwise creates the job row at status Applied
  tracker.py import-applied-history FILE    backfill from applied-history.txt lines "date | company | title | result"
  tracker.py list-applications [--company C] [--since YYYY-MM-DD]
  tracker.py companies-todo [--limit N]     companies with an application and research_status todo, oldest first
  tracker.py set-company COMPANY field=value ... (domain, careers_url, ats, email_pattern, pattern_evidence,
                                             research_status, notes)
  tracker.py add-person FILE.json           one object or a list: company, name, role, [linkedin_url, email,
                                             email_status, proof, proof_where, confidence, for_job, draft_note,
                                             draft_message, draft_email, verified_on]
  tracker.py set-person PERSON_KEY field=value ...
  tracker.py list-people [--company C]
  tracker.py counter NAME [add N]           per Eastern day counters (linkedin_loads, fills, submits)
  tracker.py export TAB                     Today | Jobs | Applied | Recruiters | Companies | Rejected | History
                                             -> {"range": "Tab!A1", "values": [[...], ...]} for update_values
  tracker.py import-edits TAB FILE.json     FILE holds the sheet values of that tab (list of rows, header first);
                                             applies the owner's edits: Jobs "Your call", Recruiters "Status",
                                             Companies "Research status"
  tracker.py run-note FILE.json             {"summary": "...", "problems": [{"company","title","text","link"}], "at": "..."}
                                             feeds the Today tab's Last run and Problems rows
  tracker.py today                          the jobs that need him, by Today section
  tracker.py phone-line HH:MM [extra]       the one line his phone gets after a run (under 200 characters)
  tracker.py portal LINK signed-up|ok|wall|verified [detail]
                                             record his account on the job's site (portals.py): signed-up = he made
                                             the account or signed in (Today mark), ok = the agent reached a form
                                             behind his sign-in, wall = the agent met a sign-in page, verified = the
                                             agent opened the site's verify-your-email link (no state change)
  tracker.py portal-of URL                  the site (portal) a URL belongs to, e.g. statestreet.wd1.myworkdayjobs.com
  tracker.py portals                        every site with an account and its state (ready, again, new)
  tracker.py stats

Keys: job_key = the link with tracking parameters removed (lower case), or company|title when there is no
link. company_key = the company name with punctuation removed, lower case, parentheticals dropped.
Statuses: New, Matched, Ready to fill, Awaiting submit, Applied, Rejected by me, Not eligible, Walled, Stale.
"""
import datetime
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portals as PT  # noqa: E402  (2026-11-10 step 3: account sites)
from titles import title_key  # noqa: E402  (2026-11-11: Jr = Junior in duplicate checks)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from runtime_paths import PRIVATE
DB_PATH = os.environ.get("TRACKER_DB") or os.path.join(PRIVATE, "tracker.sqlite")
ET = ZoneInfo("America/New_York")
STATUSES = ("New", "Matched", "Ready to fill", "Awaiting submit", "Applied", "Rejected by me",
            "Not eligible", "Walled", "Stale")
OPEN_STATUSES = ("New", "Matched", "Ready to fill", "Awaiting submit", "Walled")
KEEP_QUERY = ("gh_jid", "jobId", "jid", "id", "ashby_jid", "token", "for")

JOB_FIELDS = ("company", "title", "location", "link", "score", "eligibility", "status", "resume",
              "posted_at", "found_at", "source", "notes", "reason")
PERSON_FIELDS = ("name", "role", "linkedin_url", "email", "email_status", "proof", "proof_where",
                 "confidence", "for_job", "draft_note", "draft_message", "draft_email", "status", "verified_on")
COMPANY_FIELDS = ("domain", "careers_url", "ats", "email_pattern", "pattern_evidence", "research_status", "notes")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_key TEXT PRIMARY KEY, company TEXT NOT NULL, company_key TEXT NOT NULL, title TEXT NOT NULL,
  location TEXT DEFAULT '', link TEXT DEFAULT '', score INTEGER, eligibility TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'New', resume TEXT DEFAULT '', posted_at TEXT DEFAULT '',
  found_at TEXT NOT NULL, source TEXT DEFAULT '', notes TEXT DEFAULT '', reason TEXT DEFAULT '',
  your_call TEXT DEFAULT '', updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS applications (
  app_key TEXT PRIMARY KEY, job_key TEXT, company TEXT NOT NULL, company_key TEXT NOT NULL,
  title TEXT DEFAULT '?', link TEXT DEFAULT '', applied_at TEXT NOT NULL, submitted_by TEXT DEFAULT 'you',
  confirmed_via TEXT DEFAULT '', result TEXT DEFAULT 'applied', result_at TEXT DEFAULT '',
  source_id TEXT DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS companies (
  company_key TEXT PRIMARY KEY, company TEXT NOT NULL, domain TEXT DEFAULT '', careers_url TEXT DEFAULT '',
  ats TEXT DEFAULT '', email_pattern TEXT DEFAULT '', pattern_evidence TEXT DEFAULT '',
  research_status TEXT DEFAULT 'todo', last_researched TEXT DEFAULT '', notes TEXT DEFAULT '',
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS people (
  person_key TEXT PRIMARY KEY, company_key TEXT NOT NULL, company TEXT NOT NULL, name TEXT NOT NULL,
  role TEXT DEFAULT '', linkedin_url TEXT DEFAULT '', email TEXT DEFAULT '', email_status TEXT DEFAULT 'none',
  proof TEXT DEFAULT '', proof_where TEXT DEFAULT '', confidence TEXT DEFAULT 'low', for_job TEXT DEFAULT '',
  draft_note TEXT DEFAULT '', draft_message TEXT DEFAULT '', draft_email TEXT DEFAULT '',
  status TEXT DEFAULT 'new', verified_on TEXT DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL,
  key TEXT DEFAULT '', detail TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS counters (day TEXT NOT NULL, name TEXT NOT NULL, value INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, name));
CREATE TABLE IF NOT EXISTS portals (portal TEXT PRIMARY KEY, signed_up_at TEXT DEFAULT '', ok_at TEXT DEFAULT '',
  wall_at TEXT DEFAULT '', detail TEXT DEFAULT '');
"""


def now_et():
    return datetime.datetime.now(ET).strftime("%Y-%m-%d %H:%M")


def today_et():
    return datetime.datetime.now(ET).strftime("%Y-%m-%d")


def norm_text(s):
    return re.sub(r"[^a-z0-9]", "", html.unescape(s or "").lower())


def company_key(name):
    name = re.sub(r"\(.*?\)", " ", html.unescape(name or ""))
    name = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|via .*)\b\.?", " ", name, flags=re.I)
    return norm_text(name)


def norm_link(u):
    u = (u or "").strip().split("#")[0]
    if not u:
        return ""
    base, _, q = u.partition("?")
    keep = [x for x in q.split("&") if x.split("=")[0] in KEEP_QUERY]
    base = base.rstrip("/")
    return (base + ("?" + "&".join(keep) if keep else "")).lower()


def job_key_for(link, company, title):
    nl = norm_link(link)
    if nl and not re.search(r"linkedin\.com/jobs/search|joinhandshake\.com/job-search|ziprecruiter\.com/jobs-search", nl):
        return nl
    return "co|" + company_key(company) + "|" + norm_text(title)


def person_key_for(company, name, linkedin_url):
    if linkedin_url:
        return norm_link(linkedin_url)
    return "p|" + company_key(company) + "|" + norm_text(name)


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def log(con, kind, key="", detail=""):
    con.execute("INSERT INTO events (at, kind, key, detail) VALUES (?,?,?,?)", (now_et(), kind, key, detail[:500]))


def load_json_arg(path):
    data = json.load(open(path, encoding="utf-8"))
    return data if isinstance(data, list) else [data]


def parse_assignments(args):
    out = {}
    for a in args:
        if "=" not in a:
            raise SystemExit(f"expected field=value, got {a!r}")
        k, v = a.split("=", 1)
        out[k.strip()] = v
    return out


def ensure_company(con, company):
    ck = company_key(company)
    if not ck:
        raise SystemExit("company name is empty")
    row = con.execute("SELECT company_key FROM companies WHERE company_key=?", (ck,)).fetchone()
    if not row:
        con.execute("INSERT INTO companies (company_key, company, created_at) VALUES (?,?,?)", (ck, company.strip(), now_et()))
    return ck


def titles_match(a, b):
    a, b = title_key(a), title_key(b)
    if not a or not b or a == "?" or b == "?":
        return False
    return a == b or a in b or b in a


# ---------- jobs ----------

def upsert_job(con, obj):
    company = html.unescape((obj.get("company") or "").strip())
    title = html.unescape((obj.get("title") or "").strip())
    if not company or not title:
        raise SystemExit("a job needs company and title")
    link = (obj.get("link") or "").strip()
    key = job_key_for(link, company, title)
    status = obj.get("status") or "New"
    if status not in STATUSES:
        raise SystemExit(f"unknown status {status!r}")
    existing = con.execute("SELECT * FROM jobs WHERE job_key=?", (key,)).fetchone()
    if not existing:
        # the same company and title under another link is the same job
        dup = con.execute("SELECT * FROM jobs WHERE company_key=?", (company_key(company),)).fetchall()
        for d in dup:
            if titles_match(d["title"], title):
                existing = d
                key = d["job_key"]
                break
    ck = ensure_company(con, company)
    ts = now_et()
    if existing:
        sets, vals = [], []
        for f in JOB_FIELDS:
            if f in obj and obj[f] not in (None, "") and f != "status":
                sets.append(f"{f}=?")
                vals.append(obj[f])
        if sets:
            vals += [ts, key]
            con.execute(f"UPDATE jobs SET {', '.join(sets)}, updated_at=? WHERE job_key=?", vals)
        return key, False
    con.execute(
        "INSERT INTO jobs (job_key, company, company_key, title, location, link, score, eligibility, status, resume,"
        " posted_at, found_at, source, notes, reason, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, company, ck, title, obj.get("location") or "", link, obj.get("score"), obj.get("eligibility") or "",
         status, obj.get("resume") or "", obj.get("posted_at") or "", obj.get("found_at") or ts,
         obj.get("source") or "", obj.get("notes") or "", obj.get("reason") or "", ts))
    log(con, "job-added", key, f"{company} - {title} [{status}]")
    return key, True


def set_job(con, key, fields):
    row = con.execute("SELECT * FROM jobs WHERE job_key=?", (key,)).fetchone()
    if not row:
        raise SystemExit(f"no job {key}")
    allowed = set(JOB_FIELDS) | {"your_call"}
    for f, v in fields.items():
        if f not in allowed:
            raise SystemExit(f"cannot set {f}")
        if f == "status" and v not in STATUSES:
            raise SystemExit(f"unknown status {v!r}")
        if f == "score":
            v = int(v)
        con.execute(f"UPDATE jobs SET {f}=?, updated_at=? WHERE job_key=?", (v, now_et(), key))
    log(con, "job-set", key, json.dumps(fields)[:400])


def row_dict(r):
    return {k: r[k] for k in r.keys()}


# ---------- applications ----------

def add_application(con, obj):
    company = html.unescape((obj.get("company") or "").strip())
    if not company:
        raise SystemExit("an application needs a company")
    title = html.unescape((obj.get("title") or "?").strip()) or "?"
    link = (obj.get("link") or "").strip()
    applied_at = obj.get("applied_at") or today_et()
    ck = ensure_company(con, company)
    # link the application to a job: by link, then by company and title
    job = None
    if obj.get('job_key'):
        job = con.execute('SELECT * FROM jobs WHERE job_key=?', (obj['job_key'],)).fetchone()
        if not job or job['company_key'] != ck:
            raise ValueError('dashboard application must identify its exact stored job')
    if job is None and link:
        job = con.execute("SELECT * FROM jobs WHERE job_key=?", (job_key_for(link, company, title),)).fetchone()
    if not job:
        for j in con.execute("SELECT * FROM jobs WHERE company_key=?", (ck,)).fetchall():
            if titles_match(j["title"], title):
                job = j
                break
    if not job and title == "?":
        open_rows = [j for j in con.execute("SELECT * FROM jobs WHERE company_key=? AND status IN (?,?,?,?)",
                                             (ck, "New", "Matched", "Ready to fill", "Awaiting submit")).fetchall()]
        if len(open_rows) == 1:
            job = open_rows[0]
    if not job and title == "?":
        # 2026-11-09: the task's own submit is confirmed by an email with no title an hour later; that made a second
        # "?" row. An application to this company within 2 days is this one.
        for j in con.execute("SELECT * FROM jobs WHERE company_key=? AND status='Applied' AND title<>'?'", (ck,)).fetchall():
            if con.execute("SELECT 1 FROM applications WHERE job_key=? AND abs(julianday(applied_at)-julianday(?))<=2", (j["job_key"], applied_at[:10])).fetchone():
                job = j
                break
    created_job = False
    if not job and title != "?":
        # an earlier email gave only the company ("?" title) around the same date: this is that application
        for j in con.execute("SELECT * FROM jobs WHERE company_key=? AND title='?'", (ck,)).fetchall():
            near = con.execute("SELECT 1 FROM applications WHERE job_key=? AND abs(julianday(applied_at)-julianday(?))<=2", (j["job_key"], applied_at[:10])).fetchone()
            if near:
                new_key = job_key_for(link, company, title)
                if con.execute("SELECT 1 FROM jobs WHERE job_key=?", (new_key,)).fetchone():
                    new_key = j["job_key"]
                con.execute("UPDATE jobs SET job_key=?, title=?, link=COALESCE(NULLIF(?, ''), link), updated_at=? WHERE job_key=?", (new_key, title, link, now_et(), j["job_key"]))
                con.execute("UPDATE applications SET job_key=?, title=?, link=COALESCE(NULLIF(?, ''), link), app_key=replace(app_key, ?, ?) WHERE job_key=?", (new_key, title, link, j["job_key"], new_key, j["job_key"]))
                log(con, "job-renamed", new_key, f"was {j['job_key']}: title ? -> {title}")
                job = con.execute("SELECT * FROM jobs WHERE job_key=?", (new_key,)).fetchone()
                break
    if not job:
        key, created_job = upsert_job(con, dict(company=company, title=title, link=link, status="Applied",
                                                 source=obj.get("confirmed_via") or "email",
                                                 found_at=applied_at + " 00:00" if len(applied_at) == 10 else applied_at,
                                                 notes=f"applied {applied_at} ({obj.get('submitted_by') or 'you'}), confirmed by {obj.get('confirmed_via') or 'email'}"))
        job = con.execute("SELECT * FROM jobs WHERE job_key=?", (key,)).fetchone()
    app_key = "app|" + job["job_key"] + "|" + applied_at[:10]
    existing = con.execute("SELECT * FROM applications WHERE app_key=?", (app_key,)).fetchone()
    result = obj.get("result") or "applied"
    if not existing and result != "applied":
        # a rejection, assessment or interview mail is about an application already on record: update it
        prior = con.execute("SELECT * FROM applications WHERE job_key=? ORDER BY applied_at DESC", (job["job_key"],)).fetchone()
        if prior:
            app_key, existing = prior["app_key"], prior
    if existing:
        if result != "applied" and existing["result"] != result:
            con.execute("UPDATE applications SET result=?, result_at=? WHERE app_key=?",
                        (result, obj.get("result_at") or today_et(), app_key))
            log(con, "application-result", app_key, result)
        return app_key, False, job["job_key"]
    con.execute(
        "INSERT INTO applications (app_key, job_key, company, company_key, title, link, applied_at, submitted_by,"
        " confirmed_via, result, result_at, source_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (app_key, job["job_key"], job["company"], ck, job["title"] if title == "?" else title, link or job["link"],
         applied_at, obj.get("submitted_by") or "you", obj.get("confirmed_via") or "", result,
         obj.get("result_at") or "", obj.get("source_id") or "", now_et()))
    if job["status"] != "Applied":
        con.execute("UPDATE jobs SET status='Applied', notes=?, updated_at=? WHERE job_key=?",
                    (f"applied {applied_at} ({obj.get('submitted_by') or 'you'}), confirmed by {obj.get('confirmed_via') or 'email'}; " + (job["notes"] or ""),
                     now_et(), job["job_key"]))
    log(con, "application-added", app_key, f"{company} - {title} via {obj.get('confirmed_via') or ''}")
    return app_key, True, job["job_key"]


def import_applied_history(con, path):
    added = 0
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or " | " not in line:
            continue
        parts = [p.strip() for p in line.split(" | ")]
        if len(parts) < 4:
            continue
        date, company, title, result = parts[0], parts[1], parts[2], parts[3]
        res, res_at = "applied", ""
        m = re.search(r"rejected(?: (\d{4}-\d{2}-\d{2}))?", result, re.I)
        if m:
            res, res_at = "rejected", m.group(1) or date
        elif re.search(r"assessment", result, re.I):
            res = "assessment"
        if re.search(r"started only|not applied", result, re.I):
            continue
        title = re.sub(r"\s*\((ID|two|three|second).*?\)\s*$", "", title).strip() or "?"
        submitted_by = "task" if "(auto)" in result else "you"
        _, created, _ = add_application(con, dict(company=company, title=title, applied_at=date, submitted_by=submitted_by,
                                                  confirmed_via="email", result=res, result_at=res_at))
        added += created
    return added


# ---------- companies and people ----------

def set_company(con, company, fields):
    ck = ensure_company(con, company)
    for f, v in fields.items():
        if f not in COMPANY_FIELDS:
            raise SystemExit(f"cannot set {f}")
        con.execute(f"UPDATE companies SET {f}=? WHERE company_key=?", (v, ck))
    if fields.get("research_status") == "done":
        con.execute("UPDATE companies SET last_researched=? WHERE company_key=?", (now_et(), ck))
    log(con, "company-set", ck, json.dumps(fields)[:400])


def add_person(con, obj):
    company = (obj.get("company") or "").strip()
    name = (obj.get("name") or "").strip()
    if not company or not name:
        raise SystemExit("a person needs company and name")
    url = (obj.get("linkedin_url") or "").strip()
    if url and not re.match(r"^https://www\.linkedin\.com/in/[A-Za-z0-9._%-]+/?$", url):
        raise SystemExit(f"linkedin_url must be a profile URL (linkedin.com/in/...): {url}")
    conf = obj.get("confidence") or ("low" if not url else "medium")
    if conf not in ("high", "medium", "low"):
        raise SystemExit("confidence must be high, medium or low")
    status = obj.get("status") or "new"
    if url and not obj.get("verified_on"):
        # the profile page was not read: keep the person, but only as unverified and low
        conf, status = "low", "unverified"
    if url and not (obj.get("proof") or "").strip():
        raise SystemExit("a person with a linkedin_url needs proof (a quoted phrase from the profile or a post)")
    dnc = os.path.join(PRIVATE, "do-not-contact.txt")
    if os.path.exists(dnc):
        for l in open(dnc, encoding="utf-8"):
            if l.strip() and norm_text(l.split("|")[0]) == norm_text(name):
                raise SystemExit(f"{name} is on the do-not-contact list")
    for f in ("draft_note", "draft_message", "draft_email"):
        if len(obj.get(f) or "") > (200 if f == "draft_note" else 400 if f == "draft_message" else 1200):
            raise SystemExit(f"{f} too long")
    ck = ensure_company(con, company)
    pk = person_key_for(company, name, url)
    if con.execute("SELECT 1 FROM people WHERE person_key=?", (pk,)).fetchone():
        return pk, False
    con.execute(
        "INSERT INTO people (person_key, company_key, company, name, role, linkedin_url, email, email_status, proof,"
        " proof_where, confidence, for_job, draft_note, draft_message, draft_email, status, verified_on, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pk, ck, company, name, obj.get("role") or "", url, obj.get("email") or "", obj.get("email_status") or ("none" if not obj.get("email") else "guessed"),
         obj.get("proof") or "", obj.get("proof_where") or "", conf, obj.get("for_job") or "", obj.get("draft_note") or "",
         obj.get("draft_message") or "", obj.get("draft_email") or "", status, obj.get("verified_on") or "", now_et()))
    log(con, "person-added", pk, f"{company}: {name}, {obj.get('role') or ''} [{conf}]")
    return pk, True


def set_person(con, pk, fields):
    if not con.execute("SELECT 1 FROM people WHERE person_key=?", (pk,)).fetchone():
        raise SystemExit(f"no person {pk}")
    for f, v in fields.items():
        if f not in PERSON_FIELDS:
            raise SystemExit(f"cannot set {f}")
        con.execute(f"UPDATE people SET {f}=? WHERE person_key=?", (v, pk))
    log(con, "person-set", pk, json.dumps(fields)[:400])


# ---------- counters ----------

def counter(con, name, add=0):
    day = today_et()
    if add:
        con.execute("INSERT INTO counters (day, name, value) VALUES (?,?,?) ON CONFLICT(day, name) DO UPDATE SET value=value+?",
                    (day, name, add, add))
    row = con.execute("SELECT value FROM counters WHERE day=? AND name=?", (day, name)).fetchone()
    return {"day": day, "name": name, "value": row["value"] if row else 0}


# ---------- export and edits ----------

HEADERS = {
    "Today": ["Section", "Company", "Role", "Do this", "Link", "Posted (ET)", "Mark done / skip", "job_key"],
    "Jobs": ["Found (ET)", "Company", "Title", "Location", "Link", "Score", "Status", "Resume", "Posted", "Source", "Notes", "Your call", "job_key"],
    "Applied": ["Date", "Company", "Role", "Link", "Submitted by", "Confirmed via", "Result", "Resume", "Recruiters", "job_key"],
    "Recruiters": ["Date", "Company", "Name", "Role", "LinkedIn", "Email", "Email status", "Proof", "Confidence", "For job", "Draft note (LinkedIn)", "Draft message", "Draft email", "Status", "Verified on", "person_key"],
    "Companies": ["Company", "Domain", "Careers URL", "ATS", "Email pattern", "Pattern evidence", "Applications", "People found", "Research status", "Last researched", "Notes"],
    "Rejected": ["Found (ET)", "Company", "Title", "Link", "Reason", "Source", "Status"],
    "History": ["Date", "Company", "Role", "Result", "Confirmed via"],
}


LAST_RUN_PATH = os.path.join(PRIVATE, "scratch", "last-run.json")


# ---------- the Today tab: only what needs him (2026-11-10 redesign, owner: "I don't know what applied and what isn't") ----------
# A job is on Today in exactly one section, or not at all:
#   Press Submit  a filled form is waiting for one click (or a CAPTCHA tick)
#   Sign up       an account site: sign in or create the account, then apply
#   One click     LinkedIn Easy Apply / Handshake quick apply only
# Anything older than TODAY_HOURS is retired to Stale by `tabs.py sweep`, so the lists stay short.
FINAL = ("Applied", "Not eligible", "Rejected by me", "Stale")
TODAY_HOURS = 72
ONE_CLICK = re.compile(r"easy apply only|quick apply only|1-click apply only", re.I)
OPEN_TABS_PATH = os.path.join(PRIVATE, "scratch", "open-tabs.json")
SECTION_ORDER = ("Press Submit", "Sign up", "One click")
SECTION_LABEL = {"Sign up": "Apply yourself"}


def form_url(notes):
    m = re.search(r"form (https?://[^\s,;]+)", notes or "")
    return m.group(1) if m else ""


def job_time(j):
    """When the posting went up, as an ET datetime (a date with no time counts from the end of that day)."""
    for raw in (j["posted_at"], j["found_at"]):
        raw = str(raw or "").strip()
        m = re.match(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}):(\d{2}))?", raw)
        if not m:
            continue
        d = datetime.datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=ET)
        if m.group(2):
            d = d.replace(hour=int(m.group(2)), minute=int(m.group(3)))
            if raw.endswith("Z") or "+00:00" in raw:
                d = d.replace(tzinfo=datetime.timezone.utc).astimezone(ET)
        else:
            d = d.replace(hour=23, minute=59)
        return d
    return None


def today_section(j):
    st, n = j["status"], j["notes"] or ""
    if st in FINAL or st == "Ready to fill":
        return None
    if st == "Awaiting submit" or (st == "Walled" and re.search(r"filled \d{4}-\d{2}-\d{2}", n, re.I)):
        return "Press Submit"
    if st == "Walled":
        return "One click" if ONE_CLICK.search(n) else "Sign up"
    if st in ("New", "Matched") and ONE_CLICK.search(n):
        return "One click"
    return None


def is_expired(j, now=None):
    t = job_time(j)
    now = now or datetime.datetime.now(ET)
    return bool(t) and (now - t).total_seconds() > TODAY_HOURS * 3600


def open_job_keys():
    try:
        return set(json.load(open(OPEN_TABS_PATH, encoding="utf-8")).get("open_job_keys", []))
    except Exception:
        return set()


def open_portal_tabs():
    """Sites where he has an application page open in Brave (open-tabs.json from tabs.py snapshot or sweep; ignored
    when older than 2 h)."""
    try:
        d = json.load(open(OPEN_TABS_PATH, encoding="utf-8"))
        at = datetime.datetime.strptime(d.get("at", ""), "%Y-%m-%d %H:%M").replace(tzinfo=ET)
        if (datetime.datetime.now(ET) - at).total_seconds() > 2 * 3600:
            return set()
        return set(d.get("portal_tabs", []))
    except Exception:
        return set()


def portal_rows(con):
    return {r["portal"]: dict(r) for r in con.execute("SELECT * FROM portals")}


def portal_states(con):
    """A function link -> 'ready' | 'again' | 'new' | 'verified' for this moment (portals.py; 'verified' = not signed in,
    but the agent opened the site's verify-your-email link)."""
    rows, tabs_open, now = portal_rows(con), open_portal_tabs(), datetime.datetime.now(ET).replace(tzinfo=None)

    def st(link):
        row = rows.get(PT.portal_of(link))
        s = PT.state(row, PT.portal_of(link) in tabs_open, now)
        return "verified" if s != "ready" and (row or {}).get("detail", "").startswith("email verified") else s
    return st


def set_portal(con, link, what, detail=""):
    p = PT.portal_of(link) if re.match(r"https?://", link or "") else (link or "").strip().lower()
    if not p:
        raise SystemExit(f"no site in {link!r}")
    con.execute("INSERT INTO portals (portal) VALUES (?) ON CONFLICT(portal) DO NOTHING", (p,))
    if what == "verified":
        # the agent opened the site's verify-your-email link: he still signs in himself, so no state time changes
        con.execute("UPDATE portals SET detail=? WHERE portal=?", (f"email verified {now_et()}" + (f": {detail}" if detail else ""), p))
    else:
        col = {"signed-up": "signed_up_at", "ok": "ok_at", "wall": "wall_at"}[what]
        con.execute(f"UPDATE portals SET {col}=?, detail=COALESCE(NULLIF(?, ''), detail) WHERE portal=?", (now_et(), detail[:300], p))
    log(con, "portal-" + what, p, detail)
    return dict(con.execute("SELECT * FROM portals WHERE portal=?", (p,)).fetchone())


def today_items(con):
    """{section: [job rows]} for jobs that need him now, newest posting first."""
    out, seen = {k: [] for k in SECTION_ORDER}, set()
    rows = sorted(con.execute("SELECT * FROM jobs WHERE status IN ('New','Matched','Awaiting submit','Walled')"),
                  key=lambda j: (SECTION_ORDER.index(today_section(j)) if today_section(j) else 9, j["updated_at"] or ""))
    for j in rows:
        sec = today_section(j)
        if not sec or is_expired(j):
            continue
        # one row per real posting: deduplicate by the provider posting ID.
        ids = re.findall(r"\d{4,}", j["link"] or "")
        key = max(ids, key=len) if ids else j["job_key"]
        if key in seen:
            continue
        seen.add(key)
        out[sec].append(j)
    for k in out:
        out[k].sort(key=lambda j: job_time(j) or datetime.datetime.min.replace(tzinfo=ET), reverse=True)
    # Sign up: jobs on the same site sit together (one account covers them), the site with the newest job first
    order = {}
    for j in out["Sign up"]:
        order.setdefault(PT.portal_of(j["link"]), len(order))
    out["Sign up"].sort(key=lambda j: order[PT.portal_of(j["link"])])
    return out


ONE_CLICK_AGENT_HOURS = 48  # past this the agent's newest-first queue may not reach a row before it drops off at 72 h


def agent_one_click(j, now=None):
    """True when the ONE CLICK stage will apply to this row (plan.py's gates, 2026-11-10), so it needs nothing from him."""
    n = j["notes"] or ""
    if not ONE_CLICK.search(n) or re.search(r"PM role: track only|match refused|one-click (sending|skipped)", n):
        return False
    if j["score"] is not None and j["score"] < 35:  # owner 2026-11-12: floor 35, weak matches applied
        return False
    t = job_time(j)
    return not t or ((now or datetime.datetime.now(ET)) - t).total_seconds() <= ONE_CLICK_AGENT_HOURS * 3600


def do_this(sec, j, is_open, pstate="new"):
    n = j["notes"] or ""
    tab = "Tab is open in Brave. " if is_open else ""
    if sec == "Press Submit":
        if re.search(r"submit result unknown", n):
            return tab + "The agent pressed Submit but saw no confirmation. Check the site or your email: if it went through, mark done; if not, press Submit."
        if re.search(r"captcha", n, re.I):
            return tab + "Tick the CAPTCHA box, then press Submit."
        needs = [x.strip() for x in re.findall(r"needs you: ([^;]+)", n) if not x.lower().startswith("form has old resume")]
        old = re.search(r"form has old resume ([^;]+?) attached; swap to ([^ ;]+)", n)
        steps = []
        if old:
            steps.append(f"Swap the resume to {old.group(2)}")
        steps += needs[:3]
        guessed = re.findall(r"guessed: ([^;=]+)=", n)
        if guessed:
            steps.append("check guessed: " + ", ".join(g.strip() for g in guessed[:3]))
        lead = tab if is_open else "Open the link (the filled tab was closed: refill with fill now, or apply there). "
        return (lead + ("; ".join(steps) + ". " if steps else "") + "Press Submit.")[:480]
    if sec == "Sign up" and re.search(r"captcha", n, re.I):
        return tab + "A CAPTCHA blocks the agent here. Open the link and apply. Mark done after."
    if sec == "Sign up":
        site = PT.label(PT.portal_of(j["link"]), j["company"])
        if pstate == "ready":
            return f"Nothing to do: you are signed in on {site}. The agent applies next run."
        if pstate == "verified":
            return f"The agent verified your email on {site}. Sign in there (Brave fills the password), then set Mark to 'signed up'."
        if pstate == "again":
            return tab + f"Sign in again on {site} (Brave fills the password). Then set Mark to 'signed up'. The agent applies within the hour."
        return tab + f"Sign in on {site}, or make the account if you have none (Brave fills or suggests the password). Then set Mark to 'signed up'. The agent applies within the hour."
    button = "Apply on Handshake" if "handshake" in (j["link"] or "") or "quick apply" in n.lower() else "Easy Apply on LinkedIn"
    if re.search(r"one-click sending", n):
        return tab + f"The agent may have sent this already: check before you press {button}. Mark done if it went through."
    if agent_one_click(j):
        return "Nothing to do: the agent applies to these (10 a day, newest first)."
    why = "Weak match, so the agent skips it" if re.search(r"weak match|score under", n) or (j["score"] is not None and j["score"] < 50) else "The agent may not reach this before it drops off"
    return tab + f"{why}. Press {button} yourself if you want it, then mark done."


def export_today(con):
    rows = [HEADERS["Today"]]
    last = {}
    try:
        last = json.load(open(LAST_RUN_PATH, encoding="utf-8"))
    except Exception:
        pass
    items, opened = today_items(con), open_job_keys()
    agent = sum(1 for j in items["One click"] if agent_one_click(j))
    pstate = portal_states(con)
    signed = sum(1 for j in items["Sign up"] if pstate(j["link"]) == "ready")
    extra = {"One click": f" (agent does {agent})" if agent else "", "Sign up": f" ({signed} signed in, agent applies)" if signed else ""}
    counts = ", ".join(f"{SECTION_LABEL.get(k, k)} {len(v)}" + extra.get(k, "") for k, v in items.items())
    rows.append(["Last run", "", "", f"{last.get('summary', 'no run recorded yet')} | Needs you: {counts}", "", last.get("at", ""), "", ""])
    for sec in SECTION_ORDER:
        for j in items[sec]:
            if sec == "One click" and agent_one_click(j):
                continue  # the agent applies to these: nothing for him (owner 2026-11-12)
            is_open = j["job_key"] in opened
            link = (form_url(j["notes"]) if sec == "Press Submit" else "") or j["link"]
            t = job_time(j)
            ps = pstate(j["link"]) if sec == "Sign up" else "new"
            rows.append([SECTION_LABEL.get(sec, sec), j["company"], j["title"], do_this(sec, j, is_open, ps), link, t.strftime("%Y-%m-%d %H:%M") if t else "", "", j["job_key"]])
    today = today_et()
    for a in con.execute("SELECT * FROM applications WHERE applied_at LIKE ? ORDER BY company", (today + "%",)):
        who = "the agent" if a["submitted_by"] == "task" else "you"
        instruction = "Nothing. Platform confirms submission; sender and exact time unknown." if a["submitted_by"] == 'unattributed' else f"Nothing. Applied by {who}."
        rows.append(["Applied today", a["company"], a["title"], instruction, a["link"], a["applied_at"], "", a["job_key"] or ""])
    for pr in last.get("problems") or []:
        rows.append(["Problems", pr.get("company", ""), pr.get("title", ""), pr.get("text", "")[:480], pr.get("link", ""), last.get("at", ""), "", ""])
    return rows


def phone_line(con, at, extra=""):
    """The one line his phone gets after a run, under 200 characters."""
    items = today_items(con)
    apps = con.execute("SELECT company FROM applications WHERE applied_at LIKE ? ORDER BY rowid DESC", (today_et() + "%",)).fetchall()

    def names(lst, k=2):
        lst = list(dict.fromkeys(lst))
        return ", ".join(lst[:k]) + (f" +{len(lst) - k}" if len(lst) > k else "")
    parts = [f"[Jobs {at}] Applied {len(apps)} today" + (f" ({names([a['company'] for a in apps])})" if apps else "")]
    pstate = portal_states(con)
    todo = {"Press Submit": items["Press Submit"], "Sign up": [j for j in items["Sign up"] if pstate(j["link"]) != "ready"]}
    for sec, label in (("Press Submit", "Submit"), ("Sign up", "Apply yourself")):
        if todo[sec]:
            parts.append(f"{label} {len(todo[sec])}: {names([j['company'] for j in todo[sec]])}")
    if items["One click"]:
        agent = sum(1 for j in items["One click"] if agent_one_click(j))
        mine = len(items["One click"]) - agent
        parts.append(f"One click {len(items['One click'])}" + (f" (agent {agent})" if agent and mine else " (agent does all)" if agent else ""))
    if len(parts) == 1:
        parts.append("Nothing needs you")
    line = ". ".join(parts) + "."
    if extra:
        line += " " + extra
    return line[:199]


def export(con, tab):
    if tab == "Today":
        rows = export_today(con)
        return {"range": "Today!A1", "values": rows, "rows": len(rows) - 1}
    rows = [HEADERS[tab]]
    if tab == "Jobs":
        for j in con.execute("SELECT * FROM jobs WHERE status IN ('New','Matched','Ready to fill','Awaiting submit','Walled') ORDER BY found_at DESC"):
            rows.append([j["found_at"], j["company"], j["title"], j["location"], j["link"], j["score"] if j["score"] is not None else "",
                         j["status"], j["resume"], j["posted_at"], j["source"], (j["notes"] or "")[:490], j["your_call"], j["job_key"]])
    elif tab == "Applied":
        for a in con.execute("SELECT a.*, j.resume AS resume FROM applications a LEFT JOIN jobs j ON j.job_key=a.job_key ORDER BY a.applied_at DESC, a.company"):
            n = con.execute("SELECT COUNT(*) FROM people WHERE company_key=?", (a["company_key"],)).fetchone()[0]
            result = a["result"] + (f" {a['result_at']}" if a["result_at"] else "")
            rows.append([a["applied_at"][:10], a["company"], a["title"], a["link"], a["submitted_by"], a["confirmed_via"], result, a["resume"] or "", n, a["job_key"] or ""])
    elif tab == "Recruiters":
        for p in con.execute("SELECT * FROM people ORDER BY created_at DESC"):
            rows.append([p["created_at"][:10], p["company"], p["name"], p["role"], p["linkedin_url"], p["email"], p["email_status"], p["proof"][:300],
                         p["confidence"], p["for_job"], p["draft_note"], p["draft_message"], p["draft_email"][:490], p["status"], p["verified_on"], p["person_key"]])
    elif tab == "Companies":
        for c in con.execute("SELECT * FROM companies ORDER BY company"):
            apps = con.execute("SELECT COUNT(*) FROM applications WHERE company_key=?", (c["company_key"],)).fetchone()[0]
            if apps == 0:
                continue
            ppl = con.execute("SELECT COUNT(*) FROM people WHERE company_key=?", (c["company_key"],)).fetchone()[0]
            rows.append([c["company"], c["domain"], c["careers_url"], c["ats"], c["email_pattern"], c["pattern_evidence"][:300], apps, ppl,
                         c["research_status"], c["last_researched"], c["notes"][:300]])
    elif tab == "Rejected":
        for j in con.execute("SELECT * FROM jobs WHERE status IN ('Not eligible','Rejected by me','Stale') ORDER BY found_at DESC LIMIT 2500"):
            rows.append([j["found_at"], j["company"], j["title"], j["link"], (j["reason"] or j["eligibility"] or "")[:300], j["source"], j["status"]])
    elif tab == "History":
        for a in con.execute("SELECT * FROM applications ORDER BY applied_at DESC, company"):
            rows.append([a["applied_at"][:10], a["company"], a["title"], a["result"] + (f" {a['result_at']}" if a["result_at"] else ""), a["confirmed_via"]])
    else:
        raise SystemExit(f"unknown tab {tab}")
    width = len(rows[0])
    rows = [[("" if v is None else v) for v in r] + [""] * (width - len(r)) for r in rows]
    return {"range": f"{tab}!A1", "values": rows, "rows": len(rows) - 1}


def import_edits(con, tab, values):
    if not values:
        return {"applied": 0}
    header = values[0]
    applied = []
    if tab == "Jobs":
        ci, ck = header.index("Your call"), header.index("job_key")
        for r in values[1:]:
            if len(r) <= max(ci, ck):
                continue
            call, key = (r[ci] or "").strip().lower(), r[ck]
            if not call or not key:
                continue
            job = con.execute("SELECT * FROM jobs WHERE job_key=?", (key,)).fetchone()
            if not job or job["your_call"] == call:
                continue
            if call in ("fill now", "fill", "apply"):
                set_job(con, key, {"status": "Ready to fill", "your_call": call})
            elif call in ("skip", "no", "reject"):
                set_job(con, key, {"status": "Rejected by me", "your_call": call})
            elif call in ("applied myself", "applied", "done", "submitted"):
                add_application(con, dict(job_key=key, company=job["company"], title=job["title"], link=job["link"], submitted_by="you", confirmed_via="dashboard"))
                set_job(con, key, {"your_call": call})
            else:
                continue
            applied.append((key, call))
    elif tab == "Today":
        ci, ck, cs = header.index("Mark done / skip"), header.index("job_key"), header.index("Section")
        for r in values[1:]:
            if len(r) <= max(ci, ck):
                continue
            mark, key, section = (r[ci] or "").strip().lower(), r[ck], (r[cs] or "").strip()
            if section not in SECTION_ORDER + ("Waiting on you",) + tuple(SECTION_LABEL.values()) or not key or not mark:
                continue
            job = con.execute("SELECT * FROM jobs WHERE job_key=?", (key,)).fetchone()
            if not job or job["status"] == "Applied":
                continue
            if mark in ("done", "submitted", "applied", "yes", "x"):
                add_application(con, dict(job_key=key, company=job["company"], title=job["title"], link=job["link"], submitted_by="you", confirmed_via="dashboard"))
                applied.append((key, "done"))
            elif mark in ("skip", "no", "reject"):
                set_job(con, key, {"status": "Rejected by me"})
                applied.append((key, "skip"))
            elif mark in ("fill now", "fill", "refill"):
                # your_call records that the owner (not the tab sweep) gave the go-ahead.
                set_job(con, key, {"status": "Ready to fill", "your_call": "fill now"})
                applied.append((key, "fill now"))
            elif mark in ("signed up", "signed in", "signup", "signed"):
                # 2026-11-10 step 3: he made the account or signed in; every open job on that site becomes the agent's
                if "signed up " + today_et() not in (job["notes"] or ""):
                    set_portal(con, job["link"], "signed-up", f"marked on Today for {job['company']}")
                    set_job(con, key, {"notes": f"you signed up {now_et()} on {PT.label(PT.portal_of(job['link']), job['company'])}; agent applies; {job['notes'] or ''}"[:1900]})
                    applied.append((key, "signed up"))
    elif tab == "Recruiters":
        ci, ck = header.index("Status"), header.index("person_key")
        for r in values[1:]:
            if len(r) <= max(ci, ck):
                continue
            st, key = (r[ci] or "").strip().lower(), r[ck]
            if key and st and st in ("new", "contacted", "replied", "skip", "stale"):
                p = con.execute("SELECT status FROM people WHERE person_key=?", (key,)).fetchone()
                if p and p["status"] != st:
                    set_person(con, key, {"status": st})
                    applied.append((key, st))
    elif tab == "Companies":
        ci, cn = header.index("Research status"), header.index("Company")
        for r in values[1:]:
            if len(r) <= max(ci, cn):
                continue
            st, name = (r[ci] or "").strip().lower(), r[cn]
            if name and st in ("todo", "done", "skip", "redo"):
                ck2 = company_key(name)
                c = con.execute("SELECT research_status FROM companies WHERE company_key=?", (ck2,)).fetchone()
                if c and c["research_status"] != st:
                    set_company(con, name, {"research_status": "todo" if st == "redo" else st})
                    applied.append((ck2, st))
    else:
        raise SystemExit(f"no edits are read from {tab}")
    return {"applied": len(applied), "changes": applied}


def stats(con):
    out = {"jobs_by_status": {}, "applications": 0, "companies_todo": 0, "people": 0}
    for r in con.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status"):
        out["jobs_by_status"][r["status"]] = r["n"]
    out["applications"] = con.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    out["companies_todo"] = con.execute("SELECT COUNT(*) FROM companies c WHERE research_status='todo' AND EXISTS (SELECT 1 FROM applications a WHERE a.company_key=c.company_key)").fetchone()[0]
    out["people"] = con.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    out["db"] = DB_PATH
    return out


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    con = db()
    try:
        if cmd == "init":
            out = stats(con)
        elif cmd == "upsert-job":
            added = updated = 0
            keys = []
            for obj in load_json_arg(args[0]):
                k, created = upsert_job(con, obj)
                keys.append(k)
                added += created
                updated += (not created)
            out = {"added": added, "updated": updated, "keys": keys}
        elif cmd == "set-job":
            set_job(con, args[0], parse_assignments(args[1:]))
            out = row_dict(con.execute("SELECT * FROM jobs WHERE job_key=?", (args[0],)).fetchone())
        elif cmd == "get-job":
            r = con.execute("SELECT * FROM jobs WHERE job_key=?", (args[0],)).fetchone()
            out = row_dict(r) if r else None
        elif cmd == "find-job":
            opts = parse_assignments([a.lstrip("-").replace("=", "=", 1) if "=" in a else a for a in args]) if args and "=" in args[0] else {}
            # accept --company C --title T --link L
            o = {}
            i = 0
            while i < len(args):
                if args[i].startswith("--") and i + 1 < len(args):
                    o[args[i][2:]] = args[i + 1]
                    i += 2
                else:
                    i += 1
            o.update(opts)
            found = []
            if o.get("link"):
                r = con.execute("SELECT * FROM jobs WHERE job_key=?", (job_key_for(o["link"], o.get("company", ""), o.get("title", "")),)).fetchone()
                if r:
                    found.append(row_dict(r))
            if not found and o.get("company"):
                for r in con.execute("SELECT * FROM jobs WHERE company_key=?", (company_key(o["company"]),)):
                    if not o.get("title") or titles_match(r["title"], o["title"]):
                        found.append(row_dict(r))
            out = found
        elif cmd == "list-jobs":
            o, statuses = {}, []
            i = 0
            while i < len(args):
                if args[i] == "--status":
                    statuses.append(args[i + 1]); i += 2
                elif args[i].startswith("--") and i + 1 < len(args):
                    o[args[i][2:]] = args[i + 1]; i += 2
                else:
                    i += 1
            q, vals = "SELECT * FROM jobs WHERE 1=1", []
            if statuses:
                q += " AND status IN (%s)" % ",".join("?" * len(statuses)); vals += statuses
            if o.get("company"):
                q += " AND company_key=?"; vals.append(company_key(o["company"]))
            if o.get("min-score"):
                q += " AND score>=?"; vals.append(int(o["min-score"]))
            q += " ORDER BY score DESC, found_at DESC"
            if o.get("limit"):
                q += " LIMIT %d" % int(o["limit"])
            out = [row_dict(r) for r in con.execute(q, vals)]
        elif cmd == "add-application":
            res = [add_application(con, obj) for obj in load_json_arg(args[0])]
            out = {"added": sum(1 for r in res if r[1]), "keys": [r[0] for r in res], "job_keys": [r[2] for r in res]}
        elif cmd == "import-applied-history":
            out = {"added": import_applied_history(con, args[0])}
        elif cmd == "list-applications":
            o = {}
            i = 0
            while i < len(args):
                if args[i].startswith("--") and i + 1 < len(args):
                    o[args[i][2:]] = args[i + 1]; i += 2
                else:
                    i += 1
            q, vals = "SELECT * FROM applications WHERE 1=1", []
            if o.get("company"):
                q += " AND company_key=?"; vals.append(company_key(o["company"]))
            if o.get("since"):
                q += " AND applied_at>=?"; vals.append(o["since"])
            out = [row_dict(r) for r in con.execute(q + " ORDER BY applied_at DESC", vals)]
        elif cmd == "companies-todo":
            limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 50
            out = []
            for c in con.execute("SELECT c.* FROM companies c WHERE c.research_status='todo' AND EXISTS (SELECT 1 FROM applications a WHERE a.company_key=c.company_key AND a.result<>'rejected') ORDER BY (SELECT MAX(applied_at) FROM applications a WHERE a.company_key=c.company_key) DESC LIMIT ?", (limit,)):
                apps = [row_dict(a) for a in con.execute("SELECT title, link, applied_at, result FROM applications WHERE company_key=? ORDER BY applied_at DESC", (c["company_key"],))]
                d = row_dict(c)
                d["applications"] = apps
                d["people"] = con.execute("SELECT COUNT(*) FROM people WHERE company_key=?", (c["company_key"],)).fetchone()[0]
                out.append(d)
        elif cmd == "set-company":
            set_company(con, args[0], parse_assignments(args[1:]))
            out = row_dict(con.execute("SELECT * FROM companies WHERE company_key=?", (company_key(args[0]),)).fetchone())
        elif cmd == "add-person":
            res = [add_person(con, obj) for obj in load_json_arg(args[0])]
            out = {"added": sum(1 for r in res if r[1]), "keys": [r[0] for r in res]}
        elif cmd == "set-person":
            set_person(con, args[0], parse_assignments(args[1:]))
            out = row_dict(con.execute("SELECT * FROM people WHERE person_key=?", (args[0],)).fetchone())
        elif cmd == "list-people":
            if "--company" in args:
                out = [row_dict(r) for r in con.execute("SELECT * FROM people WHERE company_key=?", (company_key(args[args.index("--company") + 1]),))]
            else:
                out = [row_dict(r) for r in con.execute("SELECT * FROM people ORDER BY created_at DESC")]
        elif cmd == "counter":
            add = int(args[2]) if len(args) >= 3 and args[1] == "add" else 0
            out = counter(con, args[0], add)
        elif cmd == "export":
            out = export(con, args[0])
        elif cmd == "import-edits":
            out = import_edits(con, args[0], json.load(open(args[1], encoding="utf-8")))
        elif cmd == "run-note":
            data = load_json_arg(args[0])[0]
            data.setdefault("at", now_et())
            os.makedirs(os.path.dirname(LAST_RUN_PATH), exist_ok=True)
            json.dump(data, open(LAST_RUN_PATH, "w", encoding="utf-8"), indent=0)
            log(con, "run", data.get("at", ""), data.get("summary", "")[:400])
            out = {"saved": LAST_RUN_PATH}
        elif cmd == "phone-line":
            out = {"line": phone_line(con, args[0] if args else now_et()[11:], " ".join(args[1:]))}
        elif cmd == "today":
            out = {k: [{"company": j["company"], "title": j["title"], "job_key": j["job_key"]} for j in v] for k, v in today_items(con).items()}
        elif cmd == "portal":
            if len(args) < 2 or args[1] not in ("signed-up", "ok", "wall", "verified"):
                raise SystemExit("usage: portal LINK signed-up|ok|wall|verified [detail]")
            out = set_portal(con, args[0], args[1], " ".join(args[2:]))
        elif cmd == "portal-of":
            out = {"portal": PT.portal_of(args[0]), "label": PT.label(PT.portal_of(args[0]))}
        elif cmd == "portals":
            tabs_open, now = open_portal_tabs(), datetime.datetime.now(ET).replace(tzinfo=None)
            out = [dict(r, state=PT.state(r, r["portal"] in tabs_open, now), label=PT.label(r["portal"])) for r in portal_rows(con).values()]
        elif cmd == "stats":
            out = stats(con)
        else:
            raise SystemExit(f"unknown command {cmd}")
        con.commit()
    finally:
        con.close()
    print(json.dumps(out, ensure_ascii=False, indent=0 if cmd in ("export",) else None))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
