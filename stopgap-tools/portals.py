#!/usr/bin/env python3
"""Account sites ("portals") and his sign-ins on them (2026-11-10, step 3 of the autonomy build).

Owner: he presses Create / Sign in himself with Brave's password manager (Claude never creates accounts or types
passwords); the agent then fills and submits every job on that site while his session lasts.

A portal is where one account works: a Workday tenant (exampled.wd1.myworkdayjobs.com), an iCIMS company site,
a UKG/UltiPro tenant, amazon.jobs (Amazon and Audible share it), Microsoft careers, or the plain host for anything else.

State of a portal (from the tracker's portals table plus his open Brave tabs):
  ready  the agent may fill there: he marked "signed up", or the agent got in since the last sign-in wall, or he has an
         apply page of that site open in Brave and the agent has not hit a wall there in the last 6 hours
  again  he had an account but the agent hit a sign-in page since: he signs in again
  new    no account on record: he makes one
Pure functions; tracker.py and plan.py import this file.
"""
import datetime
import re
from urllib.parse import parse_qs, urlsplit

VENDORS = (("myworkdayjobs.com", "Workday"), ("myworkdaysite.com", "Workday"), ("icims.com", "iCIMS"), ("taleo.net", "Taleo"),
           ("successfactors", "SuccessFactors"), ("oraclecloud.com", "Oracle"), ("ultipro.com", "UKG"), ("paylocity.com", "Paylocity"),
           ("brassring.com", "BrassRing"), ("adp.com", "ADP"), ("smartrecruiters.com", "SmartRecruiters"))
# a tab on one of these pages means he is inside an application (signed in, or about to be)
APPLY_PAGE = re.compile(r"/apply\b|opportunityapply|/applicant/|/candidate|careers/apply|mode=apply|/userhome|/application\b|oneclick-ui", re.I)
WALL_RETRY_HOURS = 6


def portal_of(link):
    p = urlsplit((link or "").strip())
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    if not host:
        return ""
    seg = [s for s in p.path.split("/") if s]
    if host == "amazon.jobs" or host.endswith(".amazon.jobs"):
        return "amazon.jobs"
    if host.endswith("careers.microsoft.com"):
        return "careers.microsoft.com"
    if "ultipro.com" in host and seg:
        return host + "/" + seg[0].lower()
    if "successfactors" in host:
        c = parse_qs(p.query).get("company", [""])[0]
        return host + ("?company=" + c.lower() if c else "")
    return host


def label(portal, company=""):
    if portal == "amazon.jobs":
        return "amazon.jobs"
    if portal == "careers.microsoft.com":
        return "Microsoft careers"
    for mark, name in VENDORS:
        if mark in portal:
            return f"{company}'s {name} site" if company else f"{name} ({portal})"
    return portal


NOT_PORTALS = ("linkedin.com", "joinhandshake.com", "indeed.com", "ziprecruiter.com")


def is_apply_page(url):
    """An application page on a site where he may be signed in (job boards he is always signed in to are left out)."""
    host = urlsplit(url or "").netloc.lower()
    return bool(APPLY_PAGE.search(url or "")) and not any(h in host for h in NOT_PORTALS)


def _t(s):
    try:
        return datetime.datetime.strptime((s or "")[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def state(row, apply_tab_open=False, now=None):
    """'ready', 'again' or 'new' (see the module notes). row: a portals table row as a dict, or None."""
    row = row or {}
    up, ok, wall = row.get("signed_up_at") or "", row.get("ok_at") or "", row.get("wall_at") or ""
    good = max(up, ok)
    if good and (not wall or wall < good):
        return "ready"
    if apply_tab_open:
        w = _t(wall)
        now = now or datetime.datetime.now()
        if not w or (now - w).total_seconds() >= WALL_RETRY_HOURS * 3600:
            return "ready"
    return "again" if good else "new"
