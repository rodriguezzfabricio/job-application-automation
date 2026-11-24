"""Today tab and phone line (2026-11-10 redesign): each job that needs him is in exactly one section, old ones drop off."""
import datetime, json, os, subprocess, sys, tempfile, unittest
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
NOW = datetime.datetime.now(ZoneInfo("America/New_York"))
FRESH = (NOW - datetime.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
OLD = (NOW - datetime.timedelta(hours=100)).strftime("%Y-%m-%d %H:%M")


def job(company, link, **kw):
    base = dict(company=company, title="Software Engineer", link=link, score=80, status="New", posted_at=FRESH, found_at=FRESH, notes="")
    base.update(kw)
    return base


class Today(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, TRACKER_DB=os.path.join(self.d.name, "t.sqlite"))
        self.t("init")

    def tearDown(self):
        self.d.cleanup()

    def t(self, *a):
        r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "tracker.py"), *a], capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def add(self, jobs):
        f = os.path.join(self.d.name, "j.json")
        json.dump(jobs, open(f, "w"))
        self.t("upsert-job", f)

    def test_sections(self):
        self.add([
            job("FormCo", "https://jobs.ashbyhq.com/a/1", status="Awaiting submit", notes="filled 2026-11-10 10:00, tab open, form https://jobs.ashbyhq.com/a/1/application, resume x.pdf; needs you: salary"),
            job("PortalCo", "https://x.wd5.myworkdayjobs.com/a/job/1", status="Walled", notes="needs you: account required"),
            job("CaptchaCo", "https://acme.bamboohr.com/careers/1", status="Walled", notes="needs you: reCAPTCHA on form; filled 2026-11-10 11:07, tab open"),
            job("EasyCo", "https://www.linkedin.com/jobs/view/9", notes="easy apply only"),
            job("WeakCo", "https://jobs.lever.co/w/1", status="Matched", notes="weak match: shows 0 of 2"),
            job("OldPortal", "https://y.wd1.myworkdayjobs.com/b/job/2", status="Walled", posted_at=OLD, found_at=OLD),
            job("DoneCo", "https://jobs.lever.co/d/1", status="Applied"),
        ])
        got = {k: [j["company"] for j in v] for k, v in self.t("today").items()}
        self.assertEqual(sorted(got["Press Submit"]), ["CaptchaCo", "FormCo"])
        self.assertEqual(got["Sign up"], ["PortalCo"])
        self.assertEqual(got["One click"], ["EasyCo"])
        rows = self.t("export", "Today")["values"]
        sections = [r[0] for r in rows[1:]]
        self.assertEqual(sections[0], "Last run")
        self.assertNotIn("WeakCo", [r[1] for r in rows])
        self.assertNotIn("OldPortal", [r[1] for r in rows])
        form_row = next(r for r in rows if r[1] == "FormCo")
        self.assertEqual(form_row[4], "https://jobs.ashbyhq.com/a/1/application")
        self.assertIn("salary", form_row[3])
        self.assertIn("Press Submit", form_row[3])
        cap = next(r for r in rows if r[1] == "CaptchaCo")
        self.assertIn("CAPTCHA", cap[3])

    def test_phone_line(self):
        self.add([job("PortalCo", "https://x.wd5.myworkdayjobs.com/a/job/1", status="Walled"),
                  job("EasyCo", "https://www.linkedin.com/jobs/view/9", notes="easy apply only")])
        f = os.path.join(self.d.name, "a.json")
        json.dump([{"company": "Kalshi", "title": "SWE", "link": "https://jobs.ashbyhq.com/k/1", "submitted_by": "task", "confirmed_via": "confirmation page"}], open(f, "w"))
        self.t("add-application", f)
        line = self.t("phone-line", "15:07", "Check: fill 1 FAIL")["line"]
        self.assertTrue(line.startswith("[Jobs 15:07] Applied 1 today (Kalshi)."), line)
        self.assertIn("Apply yourself 1: PortalCo", line)
        self.assertIn("One click 1", line)
        self.assertTrue(line.endswith("Check: fill 1 FAIL"))
        self.assertLess(len(line), 200)

    def test_today_mark_done_records_application(self):
        self.add([job("PortalCo", "https://x.wd5.myworkdayjobs.com/a/job/1", status="Walled")])
        rows = self.t("export", "Today")["values"]
        row = next(r for r in rows if r[1] == "PortalCo")
        row[6] = "done"
        f = os.path.join(self.d.name, "edits.json")
        json.dump(rows, open(f, "w"))
        self.assertEqual(self.t("import-edits", "Today", f)["applied"], 1)
        self.assertEqual(self.t("get-job", "https://x.wd5.myworkdayjobs.com/a/job/1")["status"], "Applied")

    def test_one_click_rows_say_who_does_them(self):
        """2026-11-10: the agent applies to one-click rows, so they ask nothing of him unless the agent will skip them."""
        self.add([
            job("AgentCo", "https://www.linkedin.com/jobs/view/1", notes="easy apply only"),
            job("WeakCo", "https://www.linkedin.com/jobs/view/2", notes="easy apply only; weak match: shows 0 of 2"),
            job("SentCo", "https://www.linkedin.com/jobs/view/3", notes="one-click sending 2026-11-10 10:00; easy apply only"),
            job("UnknownCo", "https://jobs.ashbyhq.com/u/1", status="Awaiting submit", notes="needs you: submit result unknown: spinner; form https://jobs.ashbyhq.com/u/1/application"),
        ])
        values = self.t("export", "Today")["values"]
        rows = {r[1]: r for r in values}
        last_run = next(r for r in values if r[0] == "Last run")
        self.assertNotIn("AgentCo", rows)
        self.assertNotIn("WeakCo", rows)
        self.assertIn("may have sent this", rows["SentCo"][3])
        self.assertIn("Check the site", rows["UnknownCo"][3])
        self.assertIn("One click 3 (agent does 2)", last_run[3])
        line = self.t("phone-line", "10:07")["line"]
        self.assertIn("One click 3 (agent 2)", line)

    def test_sign_up_rows_follow_his_sign_in(self):
        """step 3 (2026-11-10): Sign up rows say make an account, then nothing once he marks 'signed up'."""
        a = "https://exampled.wd1.myworkdayjobs.com/Global/job/Quincy/Platform_R-1"
        self.add([
            job("Sample Bank", a, status="Walled", notes="opened for you"),
            job("Sample Motors", "https://examplee.wd503.myworkdayjobs.com/tmna/job/Plano/SWE_1", status="Walled", notes="opened for you"),
            job("Sample Bank", "https://exampled.wd1.myworkdayjobs.com/Global/job/Example-City/Data_R-2", title="Data Engineer", status="Walled", notes="opened for you"),
        ])
        values = self.t("export", "Today")["values"]
        signup = [r for r in values if r[0] == "Apply yourself"]
        self.assertEqual([r[1] for r in signup][:2], ["Sample Bank", "Sample Bank"])  # same site together
        self.assertIn("Sign in on Sample Bank's Workday site, or make the account", signup[0][3])
        row = next(r for r in values if r[7] == a.lower())
        row[6] = "signed up"
        f = os.path.join(self.d.name, "edits.json")
        json.dump(values, open(f, "w"))
        self.assertEqual(self.t("import-edits", "Today", f)["applied"], 1)
        values = self.t("export", "Today")["values"]
        texts = {r[7]: r[3] for r in values if r[0] == "Apply yourself"}
        self.assertTrue(texts[a.lower()].startswith("Nothing to do: you are signed in"))
        self.assertTrue(texts["https://exampled.wd1.myworkdayjobs.com/global/job/example-city/data_r-2"].startswith("Nothing to do"))
        self.assertIn("Apply yourself 3 (2 signed in, agent applies)", next(r for r in values if r[0] == "Last run")[3])
        line = self.t("phone-line", "10:07")["line"]
        self.assertIn("Apply yourself 1: Sample Motors", line)
        self.t("portal", a, "wall", "test")
        texts = {r[7]: r[3] for r in self.t("export", "Today")["values"] if r[0] == "Apply yourself"}
        self.assertIn("Sign in again", texts[a.lower()])
        self.t("portal", "https://examplee.wd503.myworkdayjobs.com/x", "verified")
        texts = {r[1]: r[3] for r in self.t("export", "Today")["values"] if r[0] == "Apply yourself"}
        self.assertIn("verified your email", texts["Sample Motors"])


if __name__ == "__main__":
    unittest.main()
