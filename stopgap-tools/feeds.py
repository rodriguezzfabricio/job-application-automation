#!/usr/bin/env python3
"""Scripted job discovery (2026-11-04). Reads every source that answers a plain HTTP request, applies the
title filter, the freshness window and the seen-links dedup, fetches the description of each surviving
posting, and prints one JSON report with counts per source. No browser, no judgement calls: the stage that
runs this only scores what it returns.

Sources (name: what is read)
  applyguy      github ApplyGuy/2027-New-Grad-Jobs data/new-grad-jobs.json (posted date, employer listingUrl)
  aplyr         github aplyr/New-Grad-2027 data/listings.json "open" (posted date; aplyr.ai link resolved to the employer)
  swejoblists   swejoblists.com/new-grad/2027 list, then each job page's JSON-LD (datePosted, employer apply link)
  lists         the GitHub lists in poll_lists.py (SimplifyJobs, vanshb03, speedyapply, zapply SWE, harrycodingnow, zapply broad)
  boards        Greenhouse, Lever and Ashby boards from the seed CSV, --boards-csv and boards-auto.json
  smartrecruiters  api.smartrecruiters.com postings for the company ids in boards-auto.json
  workday       <tenant>.<wd>.myworkdayjobs.com cxs search for the tenants in boards-auto.json
  wellfound     wellfound.com role pages (server-rendered listings with liveStartAt); apply needs a Wellfound login
  builtin       builtin.com entry-level list pages (ItemList JSON-LD plus card labels); apply needs a Built In account
  An aggregator posting whose company and title also came from a board in the same run takes the board's employer link.
Every posting link seen teaches boards-auto.json a new board, so the board list grows on its own.

Usage:
  feeds.py run --hours 24 --out FILE.json [--sources a,b,c] [--boards-csv FILE] [--max-descriptions 40] [--no-seen]
  feeds.py harvest FILE.json          add boards from the links in a postings file
  feeds.py boards                     print the board list and counts
Output of run: {"stats": {source: {"fetched": n, "title": n, "fresh": n, "new": n, "errors": [...]}},
                "postings": [{company, title, location, link, posted_at, source, role, description, gate_flags}]}
posted_at is ISO (UTC when the source gives a time, else the date). A posting without a date is dropped and counted.
"""
import argparse
import concurrent.futures as cf
import csv
import datetime
import html
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import title_filter  # noqa: E402

from runtime_paths import PRIVATE
PRIV = PRIVATE
SEEN_PATH = os.path.join(PRIV, "seen-links.txt")
AUTO_PATH = os.path.join(PRIV, "boards-auto.json")
SEED_CSV = os.path.join(ROOT, "docs", "stopgap", "company-boards-seed-2026-11-02.csv")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TIMEOUT = 25
DEFENSE_EMPLOYERS = r"lockheed|northrop|raytheon|\brtx\b|bae systems|general dynamics|l3harris|leidos|\bsaic\b|\bcaci\b|booz allen|\bgdit\b|mantech|peraton|parsons|anduril|shield ai|sierra nevada corp|huntington ingalls|\bmitre\b|lincoln laborator|aerospace corporation|draper|boeing defense|textron|kbr\b|amentum|jacobs|bah\b|noblis|riverside research|epirus|hermeus|applied intuition defense|naval nuclear|ngc\b"
GATE = r"us citizen|u\.s\. citizen|citizenship (?:is )?required|citizens only|security clearance|active clearance|ts/sci|top secret|secret clearance|clearance (?:is )?required|must be (?:a )?u\.?s\.? citizen|\bitar\b|\bdod\b|department of defense|public trust"
KEEP_QUERY = ("gh_jid", "jobId", "jid", "id", "ashby_jid")


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def get(url, headers=None, data=None, method=None):
    sys.path.insert(0, ROOT)
    from job_runtime.network import public_read
    return public_read(url, headers=dict({"User-Agent": UA, "Accept": "application/json, text/html;q=0.9, */*;q=0.8"}, **(headers or {})), data=data, method=method, timeout=TIMEOUT)



def get_json(url, **kw):
    body, _ = get(url, **kw)
    return json.loads(body)


def strip_html(s):
    s = html.unescape(s or "")
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", s)).strip()


def norm_link(u):
    if not (u or '').strip():
        return ''
    sys.path.insert(0, ROOT)
    from job_runtime.policy import canonical_url
    return canonical_url(u)


def norm_ct(company, title):
    return re.sub(r"[^a-z0-9]", "", (company or "").lower())[:14] + "|" + re.sub(r"[^a-z0-9]", "", (title or "").lower())[:32]


def load_seen():
    links, cts = set(), set()
    if os.path.exists(SEEN_PATH):
        for line in open(SEEN_PATH, encoding="utf-8", errors="ignore"):
            p = [x.strip() for x in line.split(" | ")]
            if len(p) >= 3:
                if p[0]:
                    links.add(norm_link(p[0]))
                cts.add(norm_ct(p[1], p[2]))
    return links, cts


def resolve(url):
    """Follows redirects with HEAD; returns the final URL without tracking parameters."""
    try:
        out = subprocess.run(["curl", "-sIL", "-m", "20", "-A", UA, "-o", "/dev/null", "-w", "%{http_code} %{url_effective}", url], capture_output=True, text=True, timeout=30).stdout.split(" ", 1)
        if len(out) == 2 and out[0] not in ("404", "410"):
            final = out[1]
            sys.path.insert(0, ROOT)
            from job_runtime.policy import canonical_url
            return canonical_url(final)
    except Exception:
        pass
    return url


def parse_date(v):
    """Returns an aware UTC datetime, or None."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        if v > 1e12:
            v = v / 1000
        return datetime.datetime.fromtimestamp(v, datetime.timezone.utc)
    s = str(v).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d"):
        try:
            d = datetime.datetime.strptime(s.replace("Z", "+0000") if "Z" in s and "%z" in fmt else s, fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=datetime.timezone.utc)
            return d
        except ValueError:
            continue
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        return datetime.datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    return None


def workday_age(text):
    """'Posted Today' -> 0 h, 'Posted Yesterday' -> 24 h, 'Posted 3 Days Ago' -> 72 h, '30+ Days Ago' -> 720."""
    t = (text or "").lower()
    if "today" in t:
        return 0
    if "yesterday" in t:
        return 24
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return int(m.group(1)) * 24
    return None


# ---------------- boards registry ----------------

def load_boards(boards_csv=None):
    boards = {}
    for path in [SEED_CSV, boards_csv]:
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if (row.get("status") or "active").strip().lower() not in ("active", ""):
                        continue
                    ats, slug = (row.get("ats") or "").strip().lower(), (row.get("board_slug") or "").strip()
                    if ats and slug:
                        boards[(ats, slug)] = {"ats": ats, "slug": slug, "company": (row.get("company") or slug).strip(), "from": os.path.basename(path)}
    if os.path.exists(AUTO_PATH):
        for b in json.load(open(AUTO_PATH, encoding="utf-8")):
            boards.setdefault((b["ats"], b["slug"]), b)
    return boards


def board_from_link(link, company=""):
    """Recognises a board in a posting link. Returns (ats, slug) or None. Workday slug = tenant/site/wd host."""
    try:
        u = urllib.parse.urlparse(link)
    except Exception:
        return None
    host, path = u.netloc.lower(), u.path
    m = re.match(r"^/([^/]+)/jobs/\d+", path)
    if host in ("job-boards.greenhouse.io", "boards.greenhouse.io") and m:
        return ("greenhouse", m.group(1))
    m = re.match(r"^/([^/]+)/", path)
    if host == "jobs.lever.co" and m:
        return ("lever", m.group(1))
    if host == "jobs.ashbyhq.com" and m:
        return ("ashby", m.group(1))
    if host == "jobs.smartrecruiters.com":
        m = re.match(r"^/(?:oneclick-ui/company/)?([^/]+)/", path)
        if m:
            return ("smartrecruiters", m.group(1))
    m = re.match(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$", host)
    if m:
        site = re.match(r"^/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)/", path)
        if site and site.group(1) not in ("job",):
            return ("workday", f"{m.group(1)}/{site.group(1)}/{m.group(2)}")
    return None


def harvest(postings, boards):
    added = 0
    auto = json.load(open(AUTO_PATH, encoding="utf-8")) if os.path.exists(AUTO_PATH) else []
    known = {(b["ats"], b["slug"]) for b in auto} | set(boards.keys())
    for p in postings:
        b = board_from_link(p.get("link", ""), p.get("company", "")) or (tuple(p["board_hint"]) if p.get("board_hint") else None)
        if b and b not in known:
            auto.append({"ats": b[0], "slug": b[1], "company": p.get("company", ""), "first_seen": now_utc().strftime("%Y-%m-%d"), "from": p.get("source", "")})
            known.add(b)
            added += 1
    json.dump(auto, open(AUTO_PATH, "w", encoding="utf-8"), indent=0)
    return added


# ---------------- sources ----------------

def src_applyguy(stats):
    out = []
    d = get_json("https://raw.githubusercontent.com/ApplyGuy/2027-New-Grad-Jobs/main/data/new-grad-jobs.json")
    items = d if isinstance(d, list) else next((v for v in d.values() if isinstance(v, list)), [])
    for i in items:
        if not isinstance(i, dict):
            continue
        out.append(dict(company=i.get("company", ""), title=i.get("title", ""), location=i.get("location", ""), link=i.get("listingUrl") or i.get("url", ""),
                        posted_at=i.get("posted", ""), source="applyguy"))
    stats["fetched"] = len(out)
    return out


def src_aplyr(stats):
    out = []
    d = get_json("https://raw.githubusercontent.com/aplyr/New-Grad-2027/main/data/listings.json")
    op = d.get("open") or {}
    items = list(op.values()) if isinstance(op, dict) else op
    for i in items:
        locs = i.get("locations") or []
        u = (i.get("url") or "").split("?")[0]
        out.append(dict(company=i.get("company", ""), title=i.get("title", ""), location=(locs[0] if locs else ""), link=u,
                        posted_at=i.get("posted") or i.get("firstSeen", ""), source="aplyr", resolve=True, mirror=u))
    stats["fetched"] = len(out)
    return out


def src_swejoblists(stats, max_pages=60, seen_links=()):
    out = []
    body, _ = get("https://swejoblists.com/new-grad/2027")
    links = []
    for u in re.findall(r'href="(https://swejoblists\.com/jobs/[^"]+|/jobs/[^"]+)"', body):
        u = u if u.startswith("http") else "https://swejoblists.com" + u
        if u not in links:
            links.append(u)
    stats["fetched"] = len(links)
    n = 0
    for u in links:
        if norm_link(u) in seen_links:
            continue
        if n >= max_pages:
            stats.setdefault("errors", []).append(f"stopped at {max_pages} job pages")
            break
        n += 1
        try:
            page, _ = get(u)
        except Exception as e:
            stats.setdefault("errors", []).append(f"{u[-40:]}: {e}"[:120])
            continue
        jp = None
        for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
            try:
                d = json.loads(m)
            except Exception:
                continue
            if d.get("@type") == "JobPosting":
                jp = d
                break
        if not jp:
            continue
        ext = [h for h in re.findall(r'href="(https?://[^"]+)"', page) if not re.search(r"swejoblists|linkedin\.com|github\.com|twitter|x\.com|discord", h)]
        org = jp.get("hiringOrganization") or {}
        loc = jp.get("jobLocation") or {}
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        addr = (loc.get("address") or {}) if isinstance(loc, dict) else {}
        location = ", ".join(x for x in [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")] if x)
        out.append(dict(company=org.get("name", "") if isinstance(org, dict) else str(org), title=jp.get("title", ""), location=location,
                        link=ext[0] if ext else u, posted_at=jp.get("datePosted", ""), source="swejoblists", mirror=u,
                        description=strip_html(jp.get("description", ""))[:8000]))
    return out


WELLFOUND_ROLES = ("software-engineer", "backend-engineer", "frontend-engineer", "full-stack-engineer", "machine-learning-engineer", "data-engineer", "devops-engineer", "product-manager")


def src_wellfound(stats, max_pages=3):
    """Wellfound role pages are server-rendered with the listings in __NEXT_DATA__ (JobListingSearchResult objects
    with liveStartAt, grouped under their StartupResult). Applying needs a Wellfound login, so the link is the
    Wellfound job page and the fill stage treats it as walled unless the employer's own board has the job."""
    out, seen = [], set()
    for role in WELLFOUND_ROLES:
        for page in range(1, max_pages + 1):
            url = f"https://wellfound.com/role/l/{role}/united-states" + (f"?page={page}" if page > 1 else "")
            try:
                body, _ = get(url)
            except Exception as e:
                stats.setdefault("errors", []).append(f"wellfound {role} p{page}: {str(e)[:60]}")
                break
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', body, re.S)
            if not m:
                stats.setdefault("errors", []).append(f"wellfound {role} p{page}: no page data (login wall?)")
                break
            try:
                nd = json.loads(m.group(1))
            except Exception:
                break
            found = 0
            data = ((nd.get("props") or {}).get("pageProps") or {}).get("apolloState", {}).get("data") or {}
            owner = {}
            for key, val in data.items():
                if isinstance(val, dict) and val.get("__typename") in ("StartupResult", "Startup") and val.get("name"):
                    for lst in val.values():
                        if isinstance(lst, list):
                            for ref in lst:
                                if isinstance(ref, dict) and str(ref.get("__ref", "")).startswith("JobListingSearchResult:"):
                                    owner[ref["__ref"]] = val["name"]
            for key, o in data.items():
                if not (isinstance(o, dict) and o.get("__typename") == "JobListingSearchResult" and o.get("id")):
                    continue
                if o["id"] in seen:
                    continue
                seen.add(o["id"])
                found += 1
                locs = o.get("locationNames") or []
                out.append(dict(company=owner.get(key, ""), title=o.get("title", ""), location=", ".join(locs[:3]) + (" (remote)" if o.get("remote") else ""),
                                link=f"https://wellfound.com/jobs/{o['id']}-{o.get('slug', 'job')}", posted_at=o.get("liveStartAt"), source="wellfound",
                                ats_hint=(o.get("atsSource") or ""), description=(o.get("description") or "")[:8000], walled="Wellfound login needed to apply"))
            if found == 0:
                break
    stats["fetched"] = len(out)
    return out


BUILTIN_PAGES = ("https://builtin.com/jobs/entry-level/dev-engineering?search=software+engineer", "https://builtin.com/jobs/entry-level/dev-engineering?search=new+grad",
                 "https://builtin.com/jobs/entry-level/dev-engineering?search=data+engineer", "https://builtin.com/jobs/entry-level/dev-engineering?search=devops",
                 "https://builtin.com/jobs/entry-level/product?search=associate+product+manager")


def builtin_age_hours(label):
    t = (label or "").lower()
    if "today" in t or "hour" in t:
        return 0
    if "yesterday" in t:
        return 24
    m = re.search(r"(\d+)\s*day", t)
    return int(m.group(1)) * 24 if m else None


def src_builtin(stats, max_pages=2):
    """Built In list pages are server-rendered. Each card starts with <my-item ... entity-id="<job id>"> and holds the
    company (data-id="company-title"), the title (data-id="job-card-title"), the posted label (after a fa-clock icon:
    "Today", "Yesterday", "N Days Ago", "Reposted ...") and the location (after a fa-location-dot icon). The ItemList
    JSON-LD gives a description per job URL. The employer link is behind a Built In account, so the link is the
    Built In job page and the row is marked walled unless a board feed in the same run has the same company and title."""
    out, seen = [], set()
    for base in BUILTIN_PAGES:
        for page in range(1, max_pages + 1):
            url = base + (f"&page={page}" if page > 1 else "")
            try:
                body, _ = get(url)
            except Exception as e:
                stats.setdefault("errors", []).append(f"builtin {base[-30:]} p{page}: {str(e)[:60]}")
                break
            items = {}
            for m in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', body, re.S):
                try:
                    d = json.loads(m)
                except Exception:
                    continue
                for x in (d.get("@graph") or [d]):
                    if isinstance(x, dict) and x.get("@type") == "ItemList":
                        for it in x.get("itemListElement") or []:
                            if it.get("url"):
                                items[it["url"].split("?")[0]] = it
            chunks = re.split(r'<my-item [^>]*entity-id="(\d+)"', body)
            found = 0
            for k in range(1, len(chunks) - 1, 2):
                jid, chunk = chunks[k], chunks[k + 1]
                if jid in seen:
                    continue
                t = re.search(r'data-id="job-card-title"[^>]*href="(/job/[^"]+)"[^>]*>([^<]+)</a>', chunk) or re.search(r'href="(/job/[^"]+)"[^>]*data-id="job-card-title"[^>]*>([^<]+)</a>', chunk)
                if not t:
                    continue
                seen.add(jid)
                found += 1
                href, title = t.group(1), html.unescape(t.group(2)).strip()
                co = re.search(r'data-id="company-title"[^>]*>\s*<span>([^<]+)</span>', chunk)
                label = re.search(r'fa-clock[^>]*></i>([^<]+)</span>', chunk)
                loc = re.search(r'fa-location-dot[^<]*(?:<[^>]+>\s*){0,8}<span class="font-barlow text-gray-04">([^<]+)</span>', chunk, re.S)
                label = html.unescape(label.group(1)).strip() if label else ""
                age = builtin_age_hours(label)
                full = "https://builtin.com" + href.split("?")[0]
                out.append(dict(company=html.unescape(co.group(1)).strip() if co else "", title=title, location=html.unescape(loc.group(1)).strip() if loc else "",
                                link=full, posted_at=(now_utc() - datetime.timedelta(hours=age)).isoformat() if age is not None else "", source="builtin",
                                description=(items.get(full, {}).get("description") or "")[:8000], reposted=bool(re.search(r"reposted", label, re.I)),
                                posted_label=label, walled="Built In account needed to apply"))
            if found == 0:
                break
    stats["fetched"] = len(out)
    return out


def prefer_employer_links(raw):
    """An aggregator posting (wellfound, builtin, aplyr) takes the employer link of a board posting with the same
    company and title read in this run, so the fill stage gets the real form instead of a login wall."""
    boards = {}
    for p in raw:
        if p.get("source", "").split("/")[0] in ("greenhouse", "lever", "ashby", "smartrecruiters", "workday", "applyguy", "swejoblists"):
            boards.setdefault(norm_ct(p.get("company"), p.get("title")), p)
    swapped = 0
    for p in raw:
        if p.get("source") in ("wellfound", "builtin", "aplyr"):
            b = boards.get(norm_ct(p.get("company"), p.get("title")))
            if b and b.get("link"):
                p["aggregator_link"] = p["link"]
                p["link"] = b["link"]
                p["source"] = p["source"] + "+" + b["source"]
                p.pop("walled", None)
                for k in ("gh", "sr", "wd"):
                    if b.get(k):
                        p[k] = b[k]
                if not p.get("posted_at") and b.get("posted_at"):
                    p["posted_at"] = b["posted_at"]
                swapped += 1
    return swapped


def src_lists(stats, hours):
    """poll_lists.py already filters titles, US, freshness and the KNOWN file; here we only pass hours."""
    known = os.path.join(PRIV, "scratch", "feeds-known.txt")
    os.makedirs(os.path.dirname(known), exist_ok=True)
    with open(known, "w", encoding="utf-8") as f:
        if os.path.exists(SEEN_PATH):
            for line in open(SEEN_PATH, encoding="utf-8", errors="ignore"):
                p = [x.strip() for x in line.split(" | ")]
                if len(p) >= 3:
                    f.write(f"{p[1]} - {p[2]}\n")
    p = subprocess.run([sys.executable, "-I", os.path.join(HERE, "poll_lists.py"), str(max(hours, 24)), known], capture_output=True, text=True, timeout=300)
    try:
        d = json.loads(p.stdout)
    except Exception:
        stats.setdefault("errors", []).append("poll_lists failed: " + (p.stderr or p.stdout)[:200])
        return []
    if d.get("failed_lists"):
        stats.setdefault("errors", []).append("lists failed: " + ",".join(d["failed_lists"]))
    out = [dict(company=r["company"], title=r["title"], location=r["location"], link=r["link"], posted_at=(now_utc() - datetime.timedelta(hours=r.get("age_hours", 0))).isoformat(), source="list:" + r.get("source", ""), age_from_list=True) for r in d.get("postings", [])]
    stats["fetched"] = len(out)
    return out


def lever_description(posting):
    parts = [posting.get('descriptionPlain') or strip_html(posting.get('description') or '')]
    if not parts[0]:
        parts.extend([posting.get('openingPlain') or strip_html(posting.get('opening') or ''),
                      posting.get('descriptionBodyPlain') or strip_html(posting.get('descriptionBody') or '')])
    for section in posting.get('lists') or []:
        parts.extend([section.get('text') or '',strip_html(section.get('content') or '')])
    parts.append(posting.get('additionalPlain') or strip_html(posting.get('additional') or ''))
    return '\n'.join(part for part in parts if part)


def fetch_board(b):
    ats, slug = b["ats"], b["slug"]
    out = []
    try:
        if ats == "greenhouse":
            d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
            for j in d.get("jobs", []):
                out.append(dict(company=b.get("company") or slug, title=j.get("title", ""), location=(j.get("location") or {}).get("name", ""), link=j.get("absolute_url", ""),
                                posted_at=j.get("first_published", ""), posted_at_source="employer.Greenhouse.first_published", source=f"greenhouse/{slug}", gh=(slug, j.get("id"))))
        elif ats == "lever":
            d = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
            for j in d:
                description=lever_description(j)
                out.append(dict(company=b.get("company") or slug, title=j.get("text", ""), location=((j.get("categories") or {}).get("location") or ""), link=j.get("hostedUrl", ""),
                                posted_at=j.get("createdAt"), posted_at_source="employer.Lever.createdAt", posted_at_url=f"https://api.lever.co/v0/postings/{slug}?mode=json", source=f"lever/{slug}", description=description, description_complete=bool(description)))
        elif ats == "ashby":
            d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false")
            for j in d.get("jobs", []):
                if j.get("isListed") is False:
                    continue
                out.append(dict(company=b.get("company") or slug, title=j.get("title", ""), location=j.get("location", ""), link=j.get("jobUrl", ""),
                                posted_at=j.get("publishedAt", ""), posted_at_source="employer.Ashby.publishedAt", posted_at_url=f"https://api.ashbyhq.com/posting-api/job-board/{slug}", source=f"ashby/{slug}", description=strip_html(j.get("descriptionHtml") or j.get("descriptionPlain") or ""), description_complete=bool(j.get("descriptionHtml") or j.get("descriptionPlain"))))
        elif ats == "smartrecruiters":
            d = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
            for j in d.get("content", []):
                loc = j.get("location") or {}
                out.append(dict(company=(j.get("company") or {}).get("name") or b.get("company") or slug, title=j.get("name", ""), location=loc.get("fullLocation") or ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x),
                                link=f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}", posted_at=j.get("releasedDate", ""), source=f"smartrecruiters/{slug}", sr=(slug, j.get("id"))))
        elif ats == "workday":
            tenant, site, wd = slug.split("/")
            base = f"https://{tenant}.{wd}.myworkdayjobs.com"
            seen_paths = set()
            for q in ("software engineer", "engineer", "developer", "new grad", "early career", "data engineer", "product manager"):
                body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q}).encode()
                d = get_json(f"{base}/wday/cxs/{tenant}/{site}/jobs", headers={"Content-Type": "application/json"}, data=body, method="POST")
                for j in d.get("jobPostings", []):
                    ep = j.get("externalPath", "")
                    if not ep or ep in seen_paths:
                        continue
                    seen_paths.add(ep)
                    age = workday_age(j.get("postedOn", ""))
                    posted = (now_utc() - datetime.timedelta(hours=age)).isoformat() if age is not None else ""
                    out.append(dict(company=b.get("company") or tenant, title=j.get("title", ""), location=j.get("locationsText", ""), link=f"{base}/en-US/{site}{ep}",
                                    posted_at=posted, source=f"workday/{tenant}", wd=(base, tenant, site, ep), posted_label=j.get("postedOn", "")))
    except Exception as e:
        return out, f"{ats}/{slug}: {str(e)[:80]}"
    return out, ""


def src_boards(stats, boards, which):
    out = []
    sel = [b for b in boards.values() if b["ats"] in which]
    stats["boards"] = len(sel)
    with cf.ThreadPoolExecutor(8) as ex:
        for res, err in ex.map(fetch_board, sel):
            out.extend(res)
            if err:
                stats.setdefault("errors", []).append(err)
    stats["fetched"] = len(out)
    return out


def employer_date(p, value, field, source_url):
    if not value or parse_date(value) is None:
        return
    if p.get("posted_at") and str(p["posted_at"]) != str(value):
        p.setdefault("listing_posted_at", p["posted_at"])
    p.update(posted_at=value, posted_at_source=field, posted_at_url=source_url)


def job_postings(value):
    if isinstance(value, list):
        for item in value:
            yield from job_postings(item)
    elif isinstance(value, dict):
        kind=value.get("@type")
        if kind=="JobPosting" or isinstance(kind,list) and "JobPosting" in kind:
            yield value
        yield from job_postings(value.get("@graph", []))


def fetch_description(p):
    try:
        if p.get("description") and p.get("description_complete"):
            return p
        if p.get("gh"):
            slug, jid = p["gh"]
            d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}")
            p["description"] = strip_html(d.get("content", ""))
            employer_date(p,d.get("first_published"),"employer.Greenhouse.first_published",f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}")
        elif p.get("sr"):
            slug, jid = p["sr"]
            d = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{jid}")
            secs = (d.get("jobAd") or {}).get("sections") or {}
            p["description"] = strip_html("\n".join(str((s or {}).get("text", "")) for s in secs.values()))
            employer_date(p,d.get("releasedDate"),"employer.SmartRecruiters.releasedDate",f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{jid}")
        elif p.get("wd"):
            base, tenant, site, ep = p["wd"]
            d = get_json(f"{base}/wday/cxs/{tenant}/{site}{ep}")
            info = d.get("jobPostingInfo") or {}
            p["description"] = strip_html(info.get("jobDescription", ""))
            locations=[info.get("location"),*(info.get("additionalLocations") or [])]
            primary_country=(info.get("country") or {}).get("descriptor") if isinstance(info.get("country"),dict) else None
            places=[s.strip() for s in locations if isinstance(s,str) and s.strip()]
            if primary_country:
                places.append(primary_country)
            if places:
                p["location"]=" / ".join(dict.fromkeys(places))
            source_url=f"{base}/wday/cxs/{tenant}/{site}{ep}"
            if info.get("startDate"):
                employer_date(p,info["startDate"],"employer.Workday.startDate",source_url)
            elif str(info.get("postedOn","")).strip().casefold() in ("today","posted today"):
                from zoneinfo import ZoneInfo
                day=now_utc().astimezone(ZoneInfo("America/New_York")).date().isoformat()
                employer_date(p,day,"employer.Workday.postedOn",source_url)
        else:
            body, final = get(p["link"])
            candidates = []
            for m in re.findall(r'''<script\b[^>]*\btype\s*=\s*["']application/ld\+json["'][^>]*>(.*?)</script\s*>''', body, re.S|re.I):
                try:
                    d = json.loads(m)
                except Exception:
                    continue
                candidates.extend(job_postings(d))
            def same_page(candidate):
                url = candidate.get('url')
                return isinstance(url,str) and norm_link(urllib.parse.urljoin(final,url)) == norm_link(final)
            matches = [item for item in candidates if same_page(item)]
            jp = matches[0] if len(matches)==1 else None
            if not matches and len(candidates)==1 and not candidates[0].get('url'):
                jp = candidates[0]
            if jp:
                p["description"] = strip_html(jp.get("description", ""))
                employer_date(p,jp.get("datePosted"),"employer.JobPosting.datePosted",final)
            else:
                p["description"] = strip_html(body)[:8000]
                p["description_quality"] = "page text, no JSON-LD"
            p["description_complete"] = bool(jp and p.get("description"))
            hint = re.search(r"job_board=([a-z0-9-]+)", body)
            if hint:
                p["board_hint"] = ("greenhouse", hint.group(1))
        if p.get("gh") or p.get("sr") or p.get("wd"):
            p["description_complete"] = bool(p.get("description"))
    except Exception as e:
        p["description_error"] = str(e)[:100]
    return p


def run(args):
    hours = args.hours
    sources = args.sources.split(",") if args.sources else ["boards", "smartrecruiters", "workday", "applyguy", "aplyr", "swejoblists", "lists", "wellfound", "builtin"]
    seen_links, seen_cts = (set(), set()) if args.no_seen else load_seen()
    boards = load_boards(args.boards_csv)
    stats, raw = {}, []
    for s in sources:
        st = stats.setdefault(s, {"fetched": 0, "title": 0, "fresh": 0, "new": 0})
        try:
            if s == "applyguy":
                raw += src_applyguy(st)
            elif s == "aplyr":
                raw += src_aplyr(st)
            elif s == "swejoblists":
                raw += src_swejoblists(st, seen_links=seen_links)
            elif s == "lists":
                raw += src_lists(st, hours)
            elif s == "boards":
                raw += src_boards(st, boards, ("greenhouse", "lever", "ashby"))
            elif s == "smartrecruiters":
                raw += src_boards(st, boards, ("smartrecruiters",))
            elif s == "workday":
                raw += src_boards(st, boards, ("workday",))
            elif s == "wellfound":
                raw += src_wellfound(st)
            elif s == "builtin":
                raw += src_builtin(st)
        except Exception as e:
            st.setdefault("errors", []).append(str(e)[:160])
    # filters
    exact_only = getattr(args,'exact_only',False)
    swapped = 0 if exact_only else prefer_employer_links(raw)
    excluded = [dict(p, exclusion_reason='reposted') for p in raw if p.get('reposted')]
    raw = [p for p in raw if not p.get("reposted")]
    cutoff = now_utc() - datetime.timedelta(hours=hours)
    kept, dropped = [], {"title": 0, "stale": 0, "nodate": 0, "seen": 0}
    batch_seen = set()
    for p in raw:
        src = p["source"].split("/")[0].split(":")[0]
        st = stats.setdefault(src if src in stats else next((k for k in stats if p["source"].startswith(k)), src), {})
        dec = title_filter.decide(p.get("title", ""), p.get("location", ""))
        if not dec["keep"]:
            dropped["title"] += 1
            excluded.append(dict(p,exclusion_reason=dec['reason']))
            continue
        st["title"] = st.get("title", 0) + 1
        p["role"] = dec["role"]
        dt = parse_date(p.get("posted_at"))
        if dt is None:
            dropped["nodate"] += 1
            excluded.append(dict(p,exclusion_reason='first posted date missing',needs_retry=True))
            continue
        if len(str(p.get("posted_at", ""))) == 10:  # date only: the day counts whole
            dt = dt + datetime.timedelta(hours=23, minutes=59)
        if dt < cutoff:
            dropped["stale"] += 1
            excluded.append(dict(p,exclusion_reason='outside freshness window'))
            continue
        st["fresh"] = st.get("fresh", 0) + 1
        if p.get("resolve"):
            p["link"] = resolve(p["link"])
            p.pop("resolve", None)
        key_l, key_ct = norm_link(p["link"]), norm_ct(p["company"], p["title"])
        if key_l in seen_links or key_l in batch_seen:
            dropped["seen"] += 1
            continue
        batch_seen.add(key_l)
        batch_seen.add(key_ct)
        st["new"] = st.get("new", 0) + 1
        kept.append(p)
    # descriptions for the survivors, bounded
    kept.sort(key=lambda p: str(p.get("posted_at")), reverse=True)
    todo = kept[: args.max_descriptions]
    if exact_only:
        # Spend some public requests on sources that supplied no original date. Rotate
        # the backlog so a perpetually undated front page cannot starve later openings.
        missing = [p for p in excluded if p.get('needs_retry') and p.get('link')]
        if missing:
            budget = max(1,args.max_descriptions//4)
            start = (int(now_utc().timestamp()//1200)*budget) % len(missing)
            rotate = (missing[start:]+missing[:start])[:budget]
            todo = todo[:max(0,args.max_descriptions-len(rotate))]+rotate
    with cf.ThreadPoolExecutor(6) as ex:
        list(ex.map(fetch_description, todo))
    for p in kept[args.max_descriptions:]:
        if not p.get("description_complete"):
            p["description_error"] = "not fetched: over the per-run limit, comes back next run"
    rejected = []
    for p in kept:
        p["gate_flags"] = sorted(set(m.lower() for m in re.findall(GATE, p.get("description", "") + " " + p.get("title", ""), re.I)))
        # Flags are evidence for role-specific interpretation, never company-name rejection.
    kept = [p for p in kept if not p.get("reject_reason")]
    for p in kept + rejected:
        for k in ("gh", "sr", "wd", "age_from_list", "resolve", "board_hint"):
            p.pop(k, None)
        if isinstance(p.get("posted_at"), (int, float)):
            p["posted_at"] = parse_date(p["posted_at"]).isoformat()
    harvested = 0 if exact_only else harvest(raw, boards)
    for p in rejected:
        p.pop("description", None)
    report = {"ran_at": now_utc().isoformat(), "hours": hours, "stats": stats, "dropped": dropped, "boards_known": len(boards), "boards_harvested": harvested, "employer_links_swapped": swapped, "kept": len(kept), "rejected_by_gate": len(rejected), "postings": kept, "rejected": rejected, "excluded": excluded}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    json.dump(report, open(args.out, "w", encoding="utf-8"), indent=0)
    summary = {k: v for k, v in report.items() if k not in ("postings","excluded")}
    summary['excluded_retained']=len(excluded)
    print(json.dumps(summary))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--hours", type=float, default=24)
    r.add_argument("--out", required=True)
    r.add_argument("--sources", default="")
    r.add_argument("--boards-csv", default="")
    r.add_argument("--max-descriptions", type=int, default=40)
    r.add_argument("--no-seen", action="store_true")
    h = sub.add_parser("harvest")
    h.add_argument("file")
    sub.add_parser("boards")
    a = ap.parse_args()
    if a.cmd == "run":
        run(a)
    elif a.cmd == "harvest":
        d = json.load(open(a.file, encoding="utf-8"))
        print(json.dumps({"added": harvest(d.get("postings", d), load_boards())}))
    elif a.cmd == "boards":
        b = load_boards()
        counts = {}
        for x in b.values():
            counts[x["ats"]] = counts.get(x["ats"], 0) + 1
        print(json.dumps({"boards": len(b), "by_ats": counts}))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
