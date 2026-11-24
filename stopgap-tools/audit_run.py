#!/usr/bin/env python3
"""Independent check of a run: what the stages actually did in the browser, counted from their transcripts, against
what they reported (2026-11-04). The owner's rule: a stage's own numbers are claims; only transcript counts are facts.

Usage:
  audit_run.py [--run DIR] [--reports FILE.json] [--json]
    DIR is a session folder under ~/.claude/projects/<project>/ holding subagents/*.jsonl; default = the newest session
    folder whose subagents were modified in the last 3 hours, other than the current one (pass --run to be exact).
    FILE.json is the run's scratch/run-<HHMM>.json ({"reports": {stage: {...}}}); default = the newest run-*.json.
Prints a table per browser stage: pages navigated, distinct job pages opened (Handshake /jobs/<id>, LinkedIn
/jobs/view/<id> or currentJobId=<id>, ZipRecruiter card clicks), form fields set, uploads, submit clicks, tabs
opened and closed, and the reported numbers beside them. A difference above 20 percent or a claim with zero
evidence is a FAIL line the coordinator must put in Problems and the phone line.
Exit 0 when every stage passes, 1 when any fails, 2 when nothing could be read.
"""
import datetime
import glob
import json
import os
import re
import sys

PROJECTS = os.path.expanduser("~/.claude/projects")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = os.path.join(ROOT, "stopgap-private", "scratch")


def project_dir():
    slug = "-" + ROOT.strip("/").replace("/", "-")
    for d in glob.glob(PROJECTS + "/*"):
        if os.path.basename(d) == slug:
            return d
    return max(glob.glob(PROJECTS + "/*"), key=os.path.getmtime)


def newest_run_dir():
    cands = []
    for d in glob.glob(project_dir() + "/*/subagents"):
        age = datetime.datetime.now().timestamp() - os.path.getmtime(d)
        if age < 3 * 3600:
            cands.append((os.path.getmtime(d), os.path.dirname(d)))
    cands.sort()
    return cands[-1][1] if cands else ""


def calls(path):
    out = []
    for line in open(path, errors="ignore"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        m = d.get("message") or {}
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    out.append((d.get("timestamp", ""), b["name"].split("__")[-1], json.dumps(b["input"])))
    return out


def count_stage(cs):
    f = {"calls": len(cs), "navigations": 0, "job_pages": set(), "card_clicks": 0, "fields_set": 0, "uploads": 0, "submit_clicks": 0,
         "tabs_opened": 0, "tabs_closed": 0, "linkedin_loads": 0, "profile_pages": set(), "people_searches": 0, "hosts": {}}
    for ts, name, s in cs:
        urls = re.findall(r'"url": "(https?://[^"]+)"', s)
        for u in urls:
            f["navigations"] += 1
            h = u.split("/")[2]
            f["hosts"][h] = f["hosts"].get(h, 0) + 1
            for pat in (r"joinhandshake\.com/jobs/(\d+)", r"linkedin\.com/jobs/view/(\d+)", r"currentJobId=(\d+)", r"indeed\.com/viewjob\?jk=([0-9a-f]+)"):
                m = re.search(pat, u)
                if m:
                    f["job_pages"].add(m.group(1))
            if "linkedin.com" in h:
                f["linkedin_loads"] += 1
                m = re.search(r"linkedin\.com/in/([^/?]+)", u)
                if m:
                    f["profile_pages"].add(m.group(1))
                if "/search/results/people" in u:
                    f["people_searches"] += 1
        f["card_clicks"] += len(re.findall(r'"action": "left_click"[^}]*"action_summary": "[^"]*(?:card|job|panel|Apply)', s))
        f["fields_set"] += s.count('"name": "form_input"') + (1 if name == "form_input" else 0)
        f["uploads"] += s.count('"name": "file_upload"') + (1 if name == "file_upload" else 0)
        f["submit_clicks"] += len(re.findall(r'"action_summary": "[^"]*(?:[Ss]ubmit|Submits)', s))
        f["tabs_opened"] += s.count("tabs_create_mcp") + (1 if name == "tabs_create_mcp" else 0)
        f["tabs_closed"] += s.count("tabs_close_mcp") + (1 if name == "tabs_close_mcp" else 0)
    f["job_pages"] = len(f["job_pages"])
    f["profile_pages"] = len(f["profile_pages"])
    return f


SEEN_PATH = os.path.join(ROOT, "stopgap-private", "seen-links.txt")
MATCHES = os.path.join(SCRATCH, "matches")


def artifacts(run_dir):
    """Facts the stages leave on disk, independent of their words: seen-links lines per source written since the run
    started, posting texts saved, tracker rows added. The run start is the subagents folder's oldest file."""
    files = glob.glob(os.path.join(run_dir, "subagents", "*.jsonl"))
    start = min(os.path.getctime(f) for f in files) if files else datetime.datetime.now().timestamp()
    day = datetime.datetime.fromtimestamp(start).strftime("%Y-%m-%d")
    out = {"seen_by_source": {}, "seen_opened_by_source": {}, "posting_files": 0, "tracker_rows_added": 0, "start": datetime.datetime.fromtimestamp(start).strftime("%H:%M")}
    if os.path.exists(SEEN_PATH):
        for line in open(SEEN_PATH, encoding="utf-8", errors="ignore"):
            p = [x.strip() for x in line.split(" | ")]
            if len(p) >= 6 and p[4] == day:
                src = p[5].split(":")[0].lower()
                out["seen_by_source"][src] = out["seen_by_source"].get(src, 0) + 1
                if "not opened" not in p[3].lower():
                    out["seen_opened_by_source"][src] = out["seen_opened_by_source"].get(src, 0) + 1
    if os.path.isdir(MATCHES):
        out["posting_files"] = sum(1 for f in glob.glob(os.path.join(MATCHES, "*.posting.txt")) if os.path.getmtime(f) >= start)
    try:
        import sqlite3
        con = sqlite3.connect(os.path.join(ROOT, "stopgap-private", "tracker.sqlite"))
        out["tracker_rows_added"] = con.execute("SELECT COUNT(*) FROM events WHERE kind='job-added' AND at>=?", (datetime.datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M"),)).fetchone()[0]
    except Exception:
        pass
    return out


def compare(stage, facts, rep, art=None):
    """Returns a list of (status, text)."""
    out = []

    def check(label, claimed, actual):
        if claimed is None:
            return
        try:
            claimed = int(claimed)
        except Exception:
            return
        if claimed == 0 and actual == 0:
            out.append(("ok", f"{label}: reported 0, transcript 0"))
        elif actual == 0 and claimed > 0:
            out.append(("FAIL", f"{label}: reported {claimed}, transcript shows 0"))
        elif claimed > actual * 1.2 + 1:
            out.append(("FAIL", f"{label}: reported {claimed}, transcript shows {actual}"))
        else:
            out.append(("ok", f"{label}: reported {claimed}, transcript {actual}"))

    if stage in ("handshake", "linkedin"):
        # a card counts as opened when its page was navigated to, or when the stage left a seen-links line for it today
        # (LinkedIn's two-pane layout opens cards with in-page clicks, which leave no navigation)
        seen_opened = (art or {}).get("seen_opened_by_source", {}).get(stage, 0)
        check("cards opened (job pages navigated or seen-links lines left)", rep.get("cards_opened"), max(facts["job_pages"] + facts["card_clicks"], seen_opened))
        check("pages read", rep.get("pages_read"), facts["navigations"])
        if facts["navigations"] == 0 and not rep.get("blocked"):
            out.append(("FAIL", "no page was opened and nothing was reported as blocked"))
        if (rep.get("rows_added") or 0) > (art or {}).get("tracker_rows_added", 0) + 0:
            out.append(("warn", f"rows_added {rep.get('rows_added')} but the tracker shows {(art or {}).get('tracker_rows_added', 0)} rows added in the run window (all stages together)"))
    elif stage == "zip_indeed":
        z = rep.get("zip") or {}
        check("ZipRecruiter cards opened", z.get("opened"), facts["card_clicks"])
    elif stage == "fill":
        n = len(rep.get("filled_for_him") or []) + len(rep.get("submitted") or [])
        check("forms filled", n, facts["uploads"])
        check("submits", len(rep.get("submitted") or []), facts["submit_clicks"])
    elif stage == "one_click":
        # an application counts only when its Submit press is in the transcript; a job counts as tried when its page was opened
        check("one-click applications (Submit presses)", len(rep.get("applied") or []), facts["submit_clicks"])
        check("jobs attempted (job pages opened)", rep.get("attempted"), facts["job_pages"])
    elif stage == "recruiters":
        people = sum(c.get("people_added", 0) for c in rep.get("companies") or [])
        check("people added (profile pages opened)", people, facts["profile_pages"])
        check("loads used", rep.get("loads_used"), facts["linkedin_loads"])
    elif stage == "intake":
        if facts["navigations"] == 0 and "gmail" not in " ".join(rep.get("blocked") or []).lower():
            out.append(("warn", "intake opened no Handshake or ZipRecruiter page"))
    for k in ("ended", "started"):
        v = rep.get(k, "")
        if v and not re.match(r"^\d\d:\d\d$", str(v)):
            out.append(("warn", f"{k} is not HH:MM: {v}"))
    return out


def main(argv):
    run = ""
    reports_file = ""
    as_json = "--json" in argv
    if "--run" in argv:
        run = argv[argv.index("--run") + 1]
    if "--reports" in argv:
        reports_file = argv[argv.index("--reports") + 1]
    run = run or newest_run_dir()
    if not run or not os.path.isdir(os.path.join(run, "subagents")):
        print(json.dumps({"error": "no run folder with subagents found"}))
        return 2
    if not reports_file:
        files = sorted(glob.glob(os.path.join(SCRATCH, "run-*.json")), key=os.path.getmtime)
        reports_file = files[-1] if files else ""
    reports = {}
    if reports_file and os.path.exists(reports_file):
        try:
            reports = (json.load(open(reports_file, encoding="utf-8")) or {}).get("reports") or {}
        except Exception:
            reports = {}
    art = artifacts(run)
    result = {"run": run, "reports_file": reports_file, "artifacts": art, "stages": {}, "fails": 0}
    for meta in sorted(glob.glob(os.path.join(run, "subagents", "*.meta.json")), key=os.path.getmtime):
        md = json.load(open(meta))
        desc = (md.get("description") or "").lower()
        desc = desc.replace("one-click", "one_click").replace("one click", "one_click")
        stage = next((s for s in ("one_click", "intake", "feeds", "handshake", "linkedin", "zip", "match", "fill", "recruiters", "mirror") if s in desc), desc[:20])
        if stage == "zip":
            stage = "zip_indeed"
        cs = calls(meta[:-10] + ".jsonl")
        facts = count_stage(cs)
        rep = reports.get(stage) or {}
        if not rep:
            for ts, name, s in cs:
                if name == "SubagentHandback":
                    try:
                        rep = json.loads(json.loads(s).get("message", "{}"))
                    except Exception:
                        rep = {}
        verdicts = compare(stage, facts, rep, art) if rep else [("warn", "no report found for this stage")]
        if not md.get("finished"):
            verdicts.append(("FAIL", "stage did not finish"))
        result["stages"][stage] = {"facts": facts, "verdicts": verdicts}
        result["fails"] += sum(1 for v in verdicts if v[0] == "FAIL")
    if as_json:
        print(json.dumps(result))
    else:
        print("artifacts since", art["start"], json.dumps({k: v for k, v in art.items() if k != "start"}))
        for st, v in result["stages"].items():
            f = v["facts"]
            print(f"{st:10s} calls {f['calls']:3d} navs {f['navigations']:3d} jobpages {f['job_pages']:3d} clicks {f['card_clicks']:3d} fields {f['fields_set']:3d} uploads {f['uploads']} submits {f['submit_clicks']} tabs +{f['tabs_opened']} -{f['tabs_closed']} hosts {f['hosts']}")
            for status, text in v["verdicts"]:
                print(f"    {status:4s} {text}")
        print("FAILS", result["fails"])
    return 1 if result["fails"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
