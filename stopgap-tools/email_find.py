#!/usr/bin/env python3
"""Resolve a business contact using a supplied format or a configured provider."""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from runtime_paths import PRIVATE
KEY_PATH = os.path.join(PRIVATE, "hunter.key")
CACHE_PATH = os.path.join(PRIVATE, "scratch", "email-cache.json")
CONFIG_PATH = os.path.join(PRIVATE, "runtime-config.json")
try:
    CONFIG = json.load(open(CONFIG_PATH, encoding="utf-8"))
except FileNotFoundError:
    CONFIG = {}
MAIL_FROM = CONFIG.get("smtp_mail_from", "")
PATTERNS = ["{first}.{last}", "{first}", "{f}{last}", "{first}{last}", "{first}_{last}", "{first}{l}", "{last}.{first}", "{f}.{last}", "{last}"]
TIMEOUT = 15


def clean(s):
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z]", "", s)
    return s


def build(pattern, first, last):
    f, l = clean(first), clean(last)
    if not f or not l:
        return ""
    return pattern.format(first=f, last=l, f=f[:1], l=l[:1])


def load_cache():
    try:
        return json.load(open(CACHE_PATH, encoding="utf-8"))
    except Exception:
        return {}


def save_cache(c):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    json.dump(c, open(CACHE_PATH, "w", encoding="utf-8"), indent=0)


def hunter_get(path, params, key):
    params = dict(params, api_key=key)
    url = "https://api.hunter.io/v2/" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "job-automation/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def infer_pattern(known, names):
    """known: list of addresses; names: list of 'First Last' in the same order."""
    votes = {}
    for addr, name in zip(known, names):
        local = addr.split("@")[0].lower()
        parts = name.split()
        if len(parts) < 2:
            continue
        first, last = parts[0], parts[-1]
        for p in PATTERNS:
            if build(p, first, last) == clean(local) or build(p, first, last) == local.replace("-", ""):
                votes[p] = votes.get(p, 0) + 1
    if not votes:
        return "", votes
    best = max(votes, key=votes.get)
    return best, votes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--first", default="")
    ap.add_argument("--last", default="")
    ap.add_argument("--pattern", default="")
    ap.add_argument("--known", default="")
    ap.add_argument("--names", default="")
    ap.add_argument("--probe-only", action="store_true")
    ap.add_argument("--no-hunter", action="store_true")
    ap.add_argument("--no-smtp", action="store_true")
    a = ap.parse_args()
    domain = a.domain.strip().lower()
    cache = load_cache()
    dom = cache.setdefault(domain, {})
    result = {"domain": domain, "email": "", "status": "none", "method": "", "pattern": "", "checks": []}

    if a.known:
        best, votes = infer_pattern([x.strip() for x in a.known.split(",") if x.strip()], [x.strip() for x in a.names.split(",")])
        if best:
            dom["pattern"] = best
            dom["pattern_evidence"] = f"inferred from {sum(votes.values())} known address(es)"
        result["pattern"] = best
        result["votes"] = votes
        if not (a.first and a.last):
            save_cache(cache)
            print(json.dumps(result))
            return 0

    if a.probe_only:
        ap.error('direct mail-server probing is unavailable')
    if not (a.first and a.last):
        print(json.dumps(dict(result, error="--first and --last are needed")))
        return 2

    pattern = a.pattern or dom.get("pattern", "")
    key = ""
    if not a.no_hunter and os.path.exists(KEY_PATH):
        key = open(KEY_PATH, encoding="utf-8").read().strip()

    candidates = []
    if pattern:
        candidates.append(build(pattern, a.first, a.last))
    for p in PATTERNS:
        c = build(p, a.first, a.last)
        if c and c not in candidates:
            candidates.append(c)
    candidates = [c + "@" + domain for c in candidates if c]

    # Step 2a: Hunter domain pattern, only when we have no pattern yet
    if key and not pattern:
        try:
            d = hunter_get("domain-search", {"domain": domain, "limit": 5}, key)
            hp = (d.get("data") or {}).get("pattern") or ""
            if hp:
                pattern = hp.replace("{first}", "{first}").replace("{last}", "{last}")
                dom["pattern"] = pattern
                dom["pattern_evidence"] = "Hunter domain-search"
                cand = build(pattern, a.first, a.last) + "@" + domain
                candidates = [cand] + [c for c in candidates if c != cand]
            result["checks"].append({"hunter_domain_search": {"pattern": hp, "emails_seen": len((d.get("data") or {}).get("emails") or [])}})
        except Exception as e:
            result["checks"].append({"hunter_domain_search_error": str(e)[:120]})

    # Step 2b: Hunter finder and verifier when still unsettled
    if key and result["status"] != "verified":
        try:
            d = hunter_get("email-finder", {"domain": domain, "first_name": a.first, "last_name": a.last}, key)
            data = d.get("data") or {}
            em, score = data.get("email"), data.get("score")
            result["checks"].append({"hunter_email_finder": {"email": em, "score": score}})
            if em:
                v = hunter_get("email-verifier", {"email": em}, key).get("data") or {}
                result["checks"].append({"hunter_email_verifier": {"status": v.get("status"), "result": v.get("result")}})
                if v.get("result") == "deliverable" or v.get("status") in ("valid",):
                    result.update(email=em, status="verified", method="hunter")
                elif v.get("status") in ("accept_all", "webmail", "unknown") or (score or 0) >= 70:
                    result.update(email=em, status="guessed", method="hunter")
        except Exception as e:
            result["checks"].append({"hunter_error": str(e)[:120]})

    if result["status"] == "none" and candidates:
        if pattern:
            result.update(email=candidates[0], status="guessed", method="pattern", pattern=pattern)
    save_cache(cache)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
