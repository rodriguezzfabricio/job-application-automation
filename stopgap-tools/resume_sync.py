#!/usr/bin/env python3
"""Synchronize the local résumé library with its configured source folder."""
import datetime, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
from runtime_paths import PRIVATE
LIB = os.path.join(PRIVATE, "my-resumes")
DB = os.environ.get("TRACKER_DB") or os.path.join(PRIVATE, "tracker.sqlite")
sys.path.insert(0, HERE)
import match_resume  # noqa: E402

BUILT_FOR = {"swe": "General software engineer roles (backend, full stack, new grad)", "ai_swe": "Software engineer roles that use AI or LLMs",
             "ai": "AI engineer and ML/LLM roles", "data": "Data engineer, data analyst, data scientist and BI roles", "forward_deployed": "Forward deployed and customer-facing engineer roles", "frontend": "Frontend roles"}
# closest fit first; a missing role falls back to the next one
TRACKS = {"ai": ["ai", "ai_swe", "swe"], "backend": ["swe", "ai_swe"], "frontend": ["frontend", "swe"], "full_stack": ["swe", "frontend", "ai_swe"],
          "forward_deployed": ["forward_deployed", "ai_swe", "swe"], "data": ["data", "swe", "ai_swe"], "general": ["swe", "ai_swe", "frontend"]}
OPEN = ("New", "Matched", "Ready to fill", "Walled", "Awaiting submit")


# Optional private mapping from PDF filenames to roles.
def _role_overrides():
    config = os.path.join(PRIVATE, 'resume-roles.json')
    if not os.path.isfile(config):
        return {}
    with open(config, encoding='utf-8') as stream:
        return {k.casefold(): v for k, v in json.load(stream).items() if v in BUILT_FOR}



def role_of(name):
    overrides = _role_overrides()
    if name.lower() in overrides:
        return overrides[name.lower()]
    t = set(re.split(r"[^a-z0-9]+", name.lower()))
    if "frontend" in t or {"front", "end"} <= t:
        return "frontend"
    if "forward" in t or "fde" in t:
        return "forward_deployed"
    if "data" in t:
        return "data"
    if "ai" in t and (t & {"swe", "software"}):
        return "ai_swe"
    if "ai" in t or "ml" in t:
        return "ai"
    return "swe"


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def pdfs(folder):
    return sorted(f for f in os.listdir(folder) if f.lower().endswith(".pdf") and os.path.isfile(os.path.join(folder, f)))


def manifest():
    p = os.path.join(LIB, "manifest.json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {"resumes": [], "tracks": {}}


def compare(source):
    lib = {r["file"]: r.get("sha256") for r in manifest().get("resumes", [])}
    src = {f: sha(os.path.join(source, f)) for f in pdfs(source)}
    return src, lib, src != lib


def build_manifest(source, files, when):
    roles = {}
    for f in files:
        r = role_of(f)
        if r in roles:
            raise ValueError(f"two files for the {r} role: {roles[r]} and {f}")
        roles[r] = f
    if "swe" not in roles:
        raise ValueError("no general software engineer resume (a file name without ai, forward or frontend in it)")
    order = [roles[r] for r in ("swe", "ai_swe", "ai", "data", "forward_deployed", "frontend") if r in roles]
    man = {"note": f"His resumes, copied unchanged from {source} on {when}. That folder is the source of truth: resume_sync.py --apply "
                   "re-copies whenever it differs. Retired copies sit in _retired-*/ and are never used.",
           "source_dir": source, "synced_at": when,
           "resumes": [{"file": f, "role": role_of(f), "built_for": BUILT_FOR[role_of(f)] + f", {when[:10]}", "sha256": sha(os.path.join(source, f)),
                        "source": os.path.join(source, f)} for f in order],
           "tracks": {t: [roles[r] for r in want if r in roles] for t, want in TRACKS.items()}}
    return man, roles


def apply(source):
    when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    files = pdfs(source)
    if not files:
        raise ValueError(f"no PDF files in {source}")
    man, roles = build_manifest(source, files, when)
    # check every file in a scratch copy first: the library is untouched when one cannot be read
    with tempfile.TemporaryDirectory() as tmp:
        for f in files:
            shutil.copy2(os.path.join(source, f), os.path.join(tmp, f))
        json.dump(man, open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8"), indent=1)
        match_resume.load_library(tmp)
    old = manifest()
    old_role = {r["file"]: r.get("role") or role_of(r["file"]) for r in old.get("resumes", [])}
    sys.path.insert(0, ROOT)
    from job_runtime.resumes import publish
    retired = publish(source, LIB, man, match_resume.load_library)
    # move open rows to the new file of the same role
    moved, waiting = 0, []
    if os.path.exists(DB):
        con = sqlite3.connect(DB)
        rows = con.execute(f"SELECT job_key, status, resume, notes, company, title FROM jobs WHERE resume<>'' AND status IN ({','.join('?' * len(OPEN))})", OPEN).fetchall()
        con.close()
        fallback = roles["swe"]
        for key, status, res, notes, company, title in rows:
            if res in files and sha(os.path.join(LIB, res)) == next((r.get("sha256") for r in old.get("resumes", []) if r["file"] == res), None):
                continue
            new = roles.get(old_role.get(res) or role_of(res), fallback)
            notes = re.sub(r"needs you: form has old resume [^;]*; swap to [^;]* before submitting; ", "", notes or "")
            notes = re.sub(r"resume swapped \d{4}-\d{2}-\d{2}[^;]*; ", "", notes)
            if status == "Awaiting submit":
                tag = f"needs you: form has old resume {res} attached; swap to {new} before submitting"
                waiting.append({"company": company, "title": title, "resume": new})
            else:
                tag = f"resume swapped {when[:10]} from {res}"
            done = subprocess.run([sys.executable, "-I", os.path.join(HERE, "tracker.py"), "set-job", key, f"resume={new}", f"notes={tag}; {notes}"[:1900]], capture_output=True, text=True)
            moved += done.returncode == 0
    return {"ok": True, "changed": True, "source": source, "files": {role_of(f): f for f in files}, "retired_to": retired, "rows_moved": moved, "forms_waiting_with_old_resume": waiting}


def main(argv):
    if not ({"--check", "--apply"} & set(argv)):
        print(__doc__)
        return 2
    source = argv[argv.index("--source") + 1] if "--source" in argv else manifest().get("source_dir", "")
    source = os.path.abspath(os.path.expanduser(source)) if source else ""
    try:
        if not source or not os.path.isdir(source):
            print(json.dumps({"ok": False, "changed": False, "error": f"source folder missing: {source or '(none recorded)'}; the library is unchanged"}))
            return 1
        src, lib, changed = compare(source)
        if "--apply" not in argv or not changed:
            print(json.dumps({"ok": True, "changed": changed, "source": source, "source_files": sorted(src), "library_files": sorted(lib)}))
            return 0
        print(json.dumps(apply(source)))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
        print(json.dumps({"ok": False, "changed": False, "error": f"{e}; the library is unchanged"}))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
