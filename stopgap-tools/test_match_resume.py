#!/usr/bin/env python3
"""Tests for match_resume.py. Uses made-up resumes, so it needs no private files. Run: python3 test_match_resume.py"""
import hashlib, json, os, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
POSTING = """We need Python and Java. You must know Kubernetes. Go is a plus. Experience with REST APIs is required.
Full-stack web work is a plus. C++ is required. Agile teams."""


def resume(skills, work):
    return f"Pat Example\npat@example.com | GitHub\nEducation\nSome University\nSkills\n{skills}\nExperience\n{work}\nProjects\nNothing else\n"


LIB = {"a.txt": resume("Languages: Python", "Built a Python service. Used JavaScript and C# daily. Let it go."),
       "b.txt": resume("Languages: Python, Java", "Built a Python service."),
       "c.txt": resume("Languages: Python", "Built Python and Java services with Kubernetes."),
       "d.txt": resume("Languages: Python", "Built a Python service.")}
TRACKS = {"backend": ["d.txt", "b.txt"], "general": []}


def req(rid, quote, terms, kind="must"):
    return dict(id=rid, label=terms[0], quote=quote, kind=kind, terms=terms)


class Match(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.lib = os.path.join(self.dir, "lib")
        os.mkdir(self.lib)

    def run_it(self, requirements, lib=None, track="backend", shas=None):
        lib = LIB if lib is None else lib
        for name, text in lib.items():
            with open(os.path.join(self.lib, name), "w") as f:
                f.write(text)
        entries = [dict(file=n, sha256=(shas or {}).get(n, "")) for n in lib]
        with open(os.path.join(self.lib, "manifest.json"), "w") as f:
            json.dump(dict(resumes=entries, tracks=TRACKS), f)
        with open(os.path.join(self.dir, "r.json"), "w") as f:
            json.dump(dict(track=track, requirements=requirements), f)
        with open(os.path.join(self.dir, "p.txt"), "w") as f:
            f.write(POSTING)
        done = subprocess.run([sys.executable, os.path.join(HERE, "match_resume.py"), self.dir + "/r.json", self.dir + "/p.txt", self.lib, self.dir + "/out"], capture_output=True, text=True)
        self.assertTrue(done.stdout, done.stderr)
        return done.returncode, json.loads(done.stdout.strip().splitlines()[-1])

    def test_most_must_haves_win(self):
        code, res = self.run_it([req("R1", "You must know Kubernetes", ["Kubernetes"]), req("R2", "We need Python", ["Python"])])
        self.assertEqual((code, res["resume"], res["must_shown"], res["must_total"]), (0, "c.txt", 2, 2))

    def test_must_haves_beat_preferred_qualifications(self):
        lib = {"a.txt": resume("Languages: Python", "Used Kubernetes."), "b.txt": resume("Languages: Python", "Used Go and full-stack.")}
        _, res = self.run_it([req("R1", "You must know Kubernetes", ["Kubernetes"]), req("R2", "Go is a plus", ["Go"], "nice"), req("R3", "Full-stack web work is a plus", ["full stack"], "nice")], lib)
        self.assertEqual(res["resume"], "a.txt")

    def test_work_beats_skills_list_on_a_tie(self):
        _, res = self.run_it([req("R1", "We need Python and Java", ["Java"])], {"b.txt": LIB["b.txt"], "c.txt": LIB["c.txt"]})
        self.assertEqual((res["resume"], res["decided_by"]), ("c.txt", "must-haves shown in his work"))

    def test_preferred_qualifications_beat_role_type(self):
        lib = {"d.txt": resume("Languages: Python", "Built things."), "b.txt": resume("Languages: Python, Go", "Built things.")}
        _, res = self.run_it([req("R1", "Go is a plus", ["Go"], "nice")], lib)
        self.assertEqual((res["resume"], res["decided_by"], res["picked_for"]), ("b.txt", "preferred qualifications", ["Go"]))

    def test_role_type_beats_duties(self):
        lib = {"d.txt": resume("Languages: Python", "Built things."), "a.txt": resume("Languages: Python, Go", "Built things.")}
        _, res = self.run_it([req("R1", "Go is a plus", ["Go"], "duty")], lib)
        self.assertEqual((res["resume"], res["decided_by"]), ("d.txt", "role type"))

    def test_duties_break_a_tie_outside_the_role_type_list(self):
        lib = {"a.txt": resume("Languages: Python", "Built things."), "z.txt": resume("Languages: Python, Go", "Built things.")}
        _, res = self.run_it([req("R1", "Go is a plus", ["Go"], "duty")], lib, track="general")
        self.assertEqual((res["resume"], res["decided_by"], res["picked_for"]), ("z.txt", "day-to-day duties", ["Go"]))

    def test_role_type_breaks_a_tie(self):
        _, res = self.run_it([req("R1", "We need Python", ["Python"])], {"a.txt": resume("Languages: Python", "Python."), "d.txt": resume("Languages: Python", "Python.")})
        self.assertEqual((res["resume"], res["decided_by"]), ("d.txt", "role type"))

    def test_first_listed_file_breaks_a_full_tie(self):
        _, res = self.run_it([req("R1", "We need Python", ["Python"])], {"a.txt": resume("Languages: Python", "Python."), "z.txt": resume("Languages: Python", "Python.")}, track="general")
        self.assertEqual((res["resume"], res["decided_by"]), ("a.txt", "newest file"))

    def test_java_is_not_javascript_and_c_is_not_c_sharp(self):
        lib = {"a.txt": LIB["a.txt"]}
        for quote, term in (("We need Python and Java", "Java"), ("C++ is required", "C"), ("C++ is required", "C++"), ("We need Python", "Script")):
            code, res = self.run_it([req("R1", quote, [term])], lib)
            self.assertEqual((code, res["must_shown"]), (4, 0), term)

    def test_go_is_not_the_word_go(self):
        code, res = self.run_it([req("R1", "Go is a plus", ["Go"])], {"a.txt": LIB["a.txt"]})
        self.assertEqual((code, res["must_shown"]), (4, 0))

    def test_plurals_and_hyphens_match(self):
        lib = {"a.txt": resume("Tools: REST APIs", "Built a REST API and a full-stack app.")}
        for quote, term in (("REST APIs is required", "REST API"), ("REST APIs is required", "REST APIs"), ("Full-stack web work", "full stack"), ("Full-stack web work", "Full-Stack")):
            code, res = self.run_it([req("R1", quote, [term])], lib)
            self.assertEqual((code, res["must_in_work"]), (0, 1), term)

    def test_contact_line_does_not_count(self):
        code, res = self.run_it([req("R1", "We need Python", ["GitHub"])], {"a.txt": LIB["a.txt"]})
        self.assertEqual((code, res["must_shown"]), (4, 0))

    def test_weak_match_names_the_best_file_and_the_gaps(self):
        reqs = [req("R1", "You must know Kubernetes", ["Kubernetes"]), req("R2", "C++ is required", ["C++"]), req("R3", "We need Python", ["Python"])]
        self.assertEqual(self.run_it(reqs, {"a.txt": LIB["a.txt"]})[0], 0)  # one of three is not weak
        code, res = self.run_it(reqs + [req("R4", "Go is a plus", ["Go"])], {"a.txt": LIB["a.txt"]})
        self.assertEqual((code, res["status"], res["resume"]), (4, "weak_match", "a.txt"))
        self.assertEqual(res["must_gaps"], ["Kubernetes", "C++", "Go"])

    def test_invalid_input_is_refused(self):
        good = req("R1", "We need Python", ["Python"])
        for bad in (dict(good, quote="ten years of Rust"), dict(good, kind="wish"), dict(good, terms=[]), dict(good, terms=["software"]), dict(good, terms=["  "]), dict(good, label="")):
            self.assertEqual(self.run_it([bad])[0], 2, bad)
        self.assertEqual(self.run_it([good], track="astronaut")[0], 2)
        self.assertEqual(self.run_it([])[0], 2)

    def test_broken_requirements_file_is_refused(self):
        self.run_it([req("R1", "We need Python", ["Python"])])
        for text in ("not json", '{"track": "backend", "requirements": ["just a string"]}', "[1, 2]"):
            with open(self.dir + "/r.json", "w") as f:
                f.write(text)
            done = subprocess.run([sys.executable, os.path.join(HERE, "match_resume.py"), self.dir + "/r.json", self.dir + "/p.txt", self.lib, self.dir + "/out"], capture_output=True, text=True)
            self.assertEqual(done.returncode, 2, text)
            self.assertEqual(json.loads(done.stdout)["status"], "invalid")

    def test_out_folder_is_created(self):
        self.run_it([req("R1", "We need Python", ["Python"])])
        done = subprocess.run([sys.executable, os.path.join(HERE, "match_resume.py"), self.dir + "/r.json", self.dir + "/p.txt", self.lib, self.dir + "/new/folder/out"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(os.path.exists(self.dir + "/new/folder/out.match.json"))

    def test_missing_file_is_a_library_problem(self):
        self.run_it([req("R1", "We need Python", ["Python"])])
        os.remove(os.path.join(self.lib, "a.txt"))
        done = subprocess.run([sys.executable, os.path.join(HERE, "match_resume.py"), self.dir + "/r.json", self.dir + "/p.txt", self.lib, self.dir + "/out"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 5)
        self.assertIn("missing file a.txt", done.stdout)

    def test_resume_without_sections_is_a_library_problem(self):
        code, res = self.run_it([req("R1", "We need Python", ["Python"])], {"a.txt": "just some words"})
        self.assertEqual((code, res["status"]), (5, "library_problem"))

    def test_changed_file_requires_manifest_revalidation(self):
        code, res = self.run_it([req("R1", "We need Python", ["Python"])], {"a.txt": LIB["a.txt"]}, shas={"a.txt": "0" * 64})
        self.assertEqual((code, res["status"]), (5, "library_problem"))
        self.assertIn("changed", str(res))

    def test_library_is_never_changed_and_result_repeats(self):
        reqs = [req("R1", "We need Python", ["Python"]), req("R2", "You must know Kubernetes", ["Kubernetes"])]
        first = self.run_it(reqs)
        digest = {n: hashlib.sha256(open(os.path.join(self.lib, n), "rb").read()).hexdigest() for n in os.listdir(self.lib)}
        second = self.run_it(reqs)
        self.assertEqual(first, second)
        self.assertEqual(digest, {n: hashlib.sha256(open(os.path.join(self.lib, n), "rb").read()).hexdigest() for n in os.listdir(self.lib)})
        self.assertEqual(sorted(os.listdir(self.lib)), ["a.txt", "b.txt", "c.txt", "d.txt", "manifest.json"])

    def test_vocabulary_lists_skill_words_only(self):
        self.run_it([req("R1", "We need Python", ["Python"])])
        done = subprocess.run([sys.executable, os.path.join(HERE, "match_resume.py"), "--vocabulary", self.lib], capture_output=True, text=True)
        self.assertEqual(done.stdout.split(), ["Java", "Python"])


if __name__ == "__main__":
    unittest.main()
