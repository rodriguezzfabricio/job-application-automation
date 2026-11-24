#!/usr/bin/env python3
"""One spelling per job title, for duplicate checks only (2026-11-11: he applied to Gen II's "Junior Full Stack
Developer" on Nov 4 and the agent applied again to its "Jr Full Stack Developer" on Nov 10, because the titles were
compared letter by letter). Row keys are not changed by this; only "is this the same job?" checks use it.
"""
import html
import re

WORDS = {"jr": "junior", "sr": "senior", "swe": "software engineer", "sde": "software development engineer",
         "eng": "engineer", "engr": "engineer", "dev": "developer", "devs": "developers", "mgr": "manager",
         "assoc": "associate", "i": "1", "ii": "2", "iii": "3", "iv": "4", "fullstack": "full stack",
         "frontend": "front end", "backend": "back end", "ml": "machine learning", "&": "and", "newgrad": "new grad"}


def title_key(s):
    s = html.unescape(s or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)                      # "(Remote)", "(Hybrid)", "(Chewy)"
    s = re.sub(r"[-_/,|:]+", " ", s)                   # full-stack -> full stack
    words = [WORDS.get(w, w) for w in re.findall(r"[a-z0-9&]+", s)]
    return re.sub(r"[^a-z0-9]", "", " ".join(words))
