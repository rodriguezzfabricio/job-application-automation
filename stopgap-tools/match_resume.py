#!/usr/bin/env python3
"""Pick the best one of his own resumes for a job. This tool never writes or changes a resume.

Usage: match_resume.py REQUIREMENTS.json POSTING.txt LIBRARY_DIR OUT_BASE
       match_resume.py --vocabulary LIBRARY_DIR

The AI step reads only the posting and writes REQUIREMENTS.json (see match-prompt.md):
  {"track": "backend",
   "requirements": [{"id": "R1", "label": "Python", "quote": "exact words from the posting",
                     "kind": "must" | "nice" | "duty", "terms": ["Python"]}]}

LIBRARY_DIR holds the PDFs and manifest.json:
  {"resumes": [{"file": "a.pdf", "built_for": "...", "sha256": "..."}],   newest first
   "tracks": {"backend": ["a.pdf", "b.pdf"], ...}}                        closest fit first

A requirement counts as shown when one of its terms is in a resume: in its work (education, experience,
projects) or only in its skills list.

Ranking, no numeric weights: 1. most must-haves shown, 2. most must-haves shown in work, 3. most preferred
qualifications shown, 4. most preferred qualifications shown in work, 5. closest role type, 6. most day-to-day
duties shown, 7. newest. Duties come after role type because they describe the job, not what it screens for.

Prints one JSON line and writes OUT_BASE.match.json with the full table.
Exit codes: 0 ok, 2 invalid requirements, 4 weak match (under a third of the must-haves shown; the best file
is still named), 5 library problem.
"""
import hashlib, json, os, re, subprocess, sys, unicodedata

MIN_MUST_SHOWN, KINDS, MAX_TERMS = 1 / 3, ("must", "nice", "duty"), 12
GENERIC = {"software", "engineer", "engineering", "experience", "code", "coding", "develop", "development", "developer",
           "build", "building", "built", "team", "teams", "system", "systems", "data", "web", "tool", "tools", "application",
           "applications", "technology", "technologies", "programming", "project", "projects", "skills", "work"}
HEADING = re.compile(r"^\s*(education|skills|technical skills|experience|projects)\s*$", re.I)


def squash(text):
    return re.sub(r"[^a-z0-9#+.]+", " ", unicodedata.normalize("NFKC", text).lower()).strip()


def tidy(text):
    """Keep case. Hyphens, dashes, slashes and line breaks all become single spaces."""
    return " ".join(re.sub(r"[-‐-―/_]+", " ", unicodedata.normalize("NFKC", text)).split())


def found(term, text):
    """Whole-word search: Java is not JavaScript, C is not C#. Terms of 1 or 2 letters keep their case, so Go is not go."""
    term = tidy(term)
    if not term:
        return False
    if len(term) > 3 and term.endswith("s"):
        term = term[:-1]
    plural = "s?" if len(term) > 2 else ""
    pattern = r"(?<![A-Za-z0-9+#.])" + re.escape(term) + plural + r"(?![A-Za-z0-9+#])"
    return re.search(pattern, text, 0 if len(term) <= 2 else re.I) is not None


def read_text(path):
    if path.endswith(".txt"):  # plain text is for the tests
        return open(path, encoding="utf-8").read()
    from pathlib import Path
    key = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    from runtime_paths import PRIVATE
    cache = Path(PRIVATE) / 'cache' / 'pdf-text-v1'
    cached = cache / (key + '.txt')
    if cached.exists():
        return cached.read_text(encoding='utf-8')
    done = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True)
    if done.returncode != 0:
        raise ValueError(f"cannot read {os.path.basename(path)}: {done.stderr.strip()[:120]}")
    cache.mkdir(parents=True, exist_ok=True)
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=cache, delete=False) as out:
        out.write(done.stdout); temporary = out.name
    os.replace(temporary, cached)
    return done.stdout


def split_sections(text):
    """Returns (skills, work). Anything above the first heading, such as the contact line, is ignored."""
    skills, work, current = [], [], None
    for line in text.splitlines():
        m = HEADING.match(line)
        if m:
            current = skills if "skills" in m.group(1).lower() else work
        elif current is not None:
            current.append(line)
    return "\n".join(skills), "\n".join(work)


def load_library(folder):
    manifest = json.load(open(os.path.join(folder, "manifest.json"), encoding="utf-8"))
    resumes, warnings = [], []
    for order, entry in enumerate(manifest["resumes"]):
        path = os.path.join(folder, entry["file"])
        if not os.path.isfile(path):
            raise ValueError(f"missing file {entry['file']}")
        sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
        if entry.get("sha256") and entry["sha256"] != sha:
            raise ValueError(f"{entry['file']} changed since it was added to the library; revalidate the manifest")
        skills, work = split_sections(read_text(path))
        if not skills.strip() or not work.strip():
            raise ValueError(f"cannot find the skills and work sections in {entry['file']}")
        resumes.append(dict(file=entry["file"], path=os.path.abspath(path), sha256=sha, built_for=entry.get("built_for", ""),
                            order=order, skills=tidy(skills), work=tidy(work)))
    return resumes, manifest.get("tracks", {}), warnings


def validate(data, posting, tracks):
    errors, flat = [], squash(posting)
    if data.get("track") not in tracks:
        errors.append(f"track must be one of {sorted(tracks)}, got {data.get('track')}")
    reqs = data.get("requirements") or []
    if not reqs:
        errors.append("no requirements")
    seen = set()
    for r in reqs:
        rid = r.get("id")
        if not rid or rid in seen:
            errors.append(f"missing or repeated id {rid}")
        seen.add(rid)
        if r.get("kind") not in KINDS:
            errors.append(f"{rid}: bad kind {r.get('kind')}")
        if not r.get("label"):
            errors.append(f"{rid}: no label")
        quote = r.get("quote", "")
        if not quote.strip() or squash(quote) not in flat:
            errors.append(f"{rid}: quote is not in the posting: {quote[:70]}")
        terms = r.get("terms") or []
        if not terms or len(terms) > MAX_TERMS:
            errors.append(f"{rid}: needs 1 to {MAX_TERMS} terms, got {len(terms)}")
        for t in terms:
            if not str(t).strip():
                errors.append(f"{rid}: empty term")
            elif tidy(str(t)).lower() in GENERIC:
                errors.append(f"{rid}: term is too general: {t}")
    return errors


def shown(requirement, resume):
    """work, listed or None."""
    if any(found(t, resume["work"]) for t in requirement["terms"]):
        return "work"
    if any(found(t, resume["skills"]) for t in requirement["terms"]):
        return "listed"
    return None


LEVELS = ("must-haves", "must-haves shown in his work", "preferred qualifications", "preferred qualifications shown in his work",
          "role type", "day-to-day duties", "newest file")


def rank(resumes, reqs, track_list):
    table = []
    for res in resumes:
        marks = {r["id"]: shown(r, res) for r in reqs}
        count = lambda kind, only_work: sum(1 for r in reqs if r["kind"] == kind and marks[r["id"]] and (marks[r["id"]] == "work" or not only_work))
        track_pos = track_list.index(res["file"]) if res["file"] in track_list else len(track_list)
        key = (count("must", False), count("must", True), count("nice", False), count("nice", True), -track_pos, count("duty", False), -res["order"])
        table.append(dict(file=res["file"], key=key, marks=marks, must_shown=key[0], must_in_work=key[1], nice_shown=key[2], nice_in_work=key[3], track_pos=track_pos, duties_shown=key[5]))
    table.sort(key=lambda t: t["key"], reverse=True)
    return table


def why(table, reqs):
    """Names what separated the winner from the runner-up, in plain words."""
    if len(table) < 2:
        return "only file", []
    win, next_up = table[0], table[1]
    level = next(i for i in range(len(LEVELS)) if win["key"][i] != next_up["key"][i])
    if level in (4, 6):
        return LEVELS[level], []
    kind = "must" if level < 2 else "nice" if level < 4 else "duty"
    need = "work" if level in (1, 3) else None
    has = lambda t, r: t["marks"][r["id"]] and (need is None or t["marks"][r["id"]] == need)
    return LEVELS[level], [r["label"] for r in reqs if r["kind"] == kind and has(win, r) and not has(next_up, r)]


def main(argv):
    if len(argv) == 3 and argv[1] == "--vocabulary":
        return vocabulary(argv[2])
    if len(argv) != 5:
        print(__doc__)
        return 2
    req_path, post_path, library, out_base = argv[1:]
    try:
        resumes, tracks, warnings = load_library(library)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
        print(json.dumps({"ok": False, "status": "library_problem", "errors": [str(e)]}))
        return 5
    posting = open(post_path, encoding="utf-8", errors="ignore").read()
    try:
        data = json.load(open(req_path, encoding="utf-8"))
        errors = validate(data, posting, tracks)
    except (OSError, ValueError, AttributeError, TypeError) as e:
        errors = [f"cannot read the requirements file: {e}"]
    if errors:
        print(json.dumps({"ok": False, "status": "invalid", "errors": errors}))
        return 2
    reqs = data["requirements"]
    if data["track"] == "data" and tracks.get("data"):
        resumes = [r for r in resumes if r["file"] == tracks["data"][0]] or resumes
    table = rank(resumes, reqs, tracks[data["track"]])
    best = table[0]
    chosen = next(r for r in resumes if r["file"] == best["file"])
    must_total = sum(1 for r in reqs if r["kind"] == "must")
    decided_by, edge = why(table, reqs)
    weak = bool(must_total) and best["must_shown"] / must_total < MIN_MUST_SHOWN
    result = {"ok": not weak, "status": "weak_match" if weak else "ok", "resume": chosen["file"], "path": chosen["path"], "sha256": chosen["sha256"],
              "track": data["track"], "must_total": must_total, "must_shown": best["must_shown"], "must_in_work": best["must_in_work"],
              "nice_total": sum(1 for r in reqs if r["kind"] == "nice"), "nice_shown": best["nice_shown"],
              "must_gaps": [r["label"] for r in reqs if r["kind"] == "must" and not best["marks"][r["id"]]],
              "must_listed_only": [r["label"] for r in reqs if r["kind"] == "must" and best["marks"][r["id"]] == "listed"],
              "decided_by": decided_by, "picked_for": edge, "runner_up": table[1]["file"] if len(table) > 1 else None, "warnings": warnings}
    report = dict(result, requirements=[{"label": r["label"], "kind": r["kind"], "terms": r["terms"], "shown_by": {t["file"]: t["marks"][r["id"]] for t in table if t["marks"][r["id"]]}} for r in reqs],
                  ranking=[{k: t[k] for k in ("file", "must_shown", "must_in_work", "nice_shown", "nice_in_work", "track_pos", "duties_shown")} for t in table])
    os.makedirs(os.path.dirname(os.path.abspath(out_base)), exist_ok=True)
    json.dump(report, open(out_base + ".match.json", "w", encoding="utf-8"), indent=1)
    print(json.dumps(result))
    return 4 if weak else 0


def vocabulary(library):
    """The wording of every skills list, so the AI step can use the same words."""
    resumes, _, _ = load_library(library)
    items = {}
    for res in resumes:
        for line in split_sections(read_text(res["path"]))[0].splitlines():
            for part in re.split(r"[,;]", line.split(":", 1)[-1]):
                part = " ".join(part.split())
                if part:
                    items.setdefault(part.lower(), part)
    print("\n".join(sorted(items.values(), key=str.lower)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
