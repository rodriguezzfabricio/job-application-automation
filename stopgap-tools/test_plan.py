"""plan.py queue rules (2026-11-09 debug): dead rows never take a match or fill place; forms the task can submit come first."""
import datetime, json, os, subprocess, sys, tempfile, unittest
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
NOW = datetime.datetime.now(ZoneInfo("America/New_York"))
FRESH = (NOW - datetime.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
OLD = (NOW - datetime.timedelta(hours=60)).strftime("%Y-%m-%d %H:%M")


def job(company, link, **kw):
    base = dict(company=company, title="Software Engineer", link=link, score=80, status="New", posted_at=FRESH, found_at=FRESH, notes="", resume="")
    base.update(kw)
    return base


class PlanQueues(unittest.TestCase):
    def plan(self, jobs, mode="FULL", **settings):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, TRACKER_DB=os.path.join(d, "t.sqlite"))
            run = lambda *a: subprocess.run([sys.executable, "-I", *a], capture_output=True, text=True, env=env)
            run(os.path.join(HERE, "tracker.py"), "init")
            jf, sf = os.path.join(d, "jobs.json"), os.path.join(d, "settings.json")
            json.dump(jobs, open(jf, "w"))
            json.dump({"fill_mode": "hourly", "max_posting_age_hours": 24, **settings}, open(sf, "w"))
            up = run(os.path.join(HERE, "tracker.py"), "upsert-job", jf)
            self.assertEqual(up.returncode, 0, up.stderr)
            out = run(os.path.join(HERE, "plan.py"), "--hour", str(NOW.hour), "--settings", sf, "--state", os.path.join(d, "none.json"), "--mode", mode, "--walled", "Example Studio,Example Media")
            self.assertEqual(out.returncode, 0, out.stderr)
            return json.loads(out.stdout)

    def test_fill_queue_is_newest_posting_first_and_fast_runs_fill_two(self):
        ago = lambda h: (NOW - datetime.timedelta(hours=h)).strftime("%Y-%m-%d %H:%M")
        rows = [
            job("Older High Co", "https://jobs.ashbyhq.com/a/1", status="Matched", resume="a.pdf", score=95, posted_at=ago(20), found_at=ago(20)),
            job("Newest Co", "https://careers.example.com/n/1", status="Matched", resume="a.pdf", score=60, posted_at=ago(0.1), found_at=ago(0.1)),
            job("Mid Co", "https://jobs.lever.co/m/1", status="Matched", resume="a.pdf", score=70, posted_at=ago(3), found_at=ago(3)),
            job("Late Co", "https://jobs.lever.co/l/1", status="Matched", resume="a.pdf", score=70, posted_at=ago(8), found_at=ago(8)),
        ]
        full = self.plan(rows)
        self.assertEqual([j["company"] for j in full["to_fill"]], ["Newest Co", "Mid Co", "Late Co"])
        self.assertIn("next run", full["skipped"]["https://jobs.ashbyhq.com/a/1"])
        fast = self.plan(rows, mode="FAST")
        self.assertEqual([j["company"] for j in fast["to_fill"]], ["Newest Co", "Mid Co"])
        self.assertEqual(fast["fills_per_run"], 2)
        self.assertEqual(len(self.plan(rows, fills_per_run=5)["to_fill"]), 4)

    def test_match_queue_skips_dead_rows_and_puts_one_page_forms_first(self):
        p = self.plan([
            job("Workday Co", "https://x.wd5.myworkdayjobs.com/a/job/1", score=95),
            job("Closed Co", "https://www.linkedin.com/jobs/view/1", notes="LinkedIn page says no longer accepting applications (2026-11-09); not matched"),
            job("Repost Co", "https://www.linkedin.com/jobs/view/2", notes="LinkedIn page says Reposted 3 days ago: do not apply (repost rule)"),
            job("Applied Co", "https://www.linkedin.com/jobs/view/3", notes="LinkedIn shows Application submitted 2 days ago"),
            job("Easy Co", "https://www.linkedin.com/jobs/view/4", notes="easy apply only"),
            job("Old Co", "https://jobs.ashbyhq.com/old/1", posted_at=OLD, found_at=OLD),
            job("Ashby Co", "https://jobs.ashbyhq.com/fresh/1", score=60),
            job("Short Link Co", "https://grnh.se/abc123", score=70),
        ])
        self.assertEqual(p["to_match"], ["https://grnh.se/abc123", "https://jobs.ashbyhq.com/fresh/1", "https://www.linkedin.com/jobs/view/4", "https://x.wd5.myworkdayjobs.com/a/job/1"])
        why = p["skipped"]
        self.assertIn("posting closed", why["https://www.linkedin.com/jobs/view/1"])
        self.assertIn("repost", why["https://www.linkedin.com/jobs/view/2"])
        self.assertIn("already shows", why["https://www.linkedin.com/jobs/view/3"])
        self.assertIn("waiting for MATCH", why["https://www.linkedin.com/jobs/view/4"])  # 2026-11-10: the agent applies after MATCH picks a resume
        self.assertIn("older than 24 h", why["https://jobs.ashbyhq.com/old/1"])

    def test_date_only_postings_and_rows_waiting_for_their_text_are_matched(self):
        yesterday = (NOW - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        two_days = (NOW - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
        p = self.plan([
            job("Dover Co", "https://jobs.dover.com/a/1", posted_at=yesterday),
            job("Stale Co", "https://jobs.dover.com/b/1", posted_at=two_days, found_at=two_days + " 09:00"),
            job("Unread Co", "https://jobs.dover.com/c/1", posted_at="", notes="no description; match stage will read the page"),
            job("Blank Co", "https://jobs.dover.com/d/1", notes="no description; employer page unreadable"),
        ])
        self.assertEqual(sorted(p["to_match"]), ["https://jobs.dover.com/a/1", "https://jobs.dover.com/c/1"])
        self.assertIn("older than 24 h", p["skipped"]["https://jobs.dover.com/b/1"])

    def test_fill_queue_takes_matched_fresh_rows_and_never_a_row_skipped_before(self):
        p = self.plan([
            job("Good Co", "https://jobs.ashbyhq.com/good/1", status="Matched", resume="a.pdf", notes="shows 4 of 4 must-haves"),
            job("Tried Co", "https://jobs.lever.co/tried/1", status="Matched", resume="a.pdf", notes="fill skipped 2026-11-09 20:10: generic list page, no form"),
            job("No Form Co", "https://aggregator.example.invalid/sample-listing", status="Matched", resume="a.pdf", notes="employer link not found"),
            job("Unverified Co", "https://jobs.dover.com/u/1", status="Matched", resume="a.pdf", score=60, notes="needs you: unverified requirements (no must-haves found in the text read); shows 0 of 0 must-haves"),
            job("His Co", "https://jobs.lever.co/his/1", status="Matched", resume="a.pdf", notes="needs you: salary"),
        ])
        self.assertEqual([j["job_key"] for j in p["to_fill"]], ["https://jobs.ashbyhq.com/good/1", "https://jobs.dover.com/u/1"])
        self.assertIn("skipped before", p["skipped"]["https://jobs.lever.co/tried/1"])
        self.assertIn("no employer form", p["skipped"]["https://aggregator.example.invalid/sample-listing"])

    def test_one_click_queue_takes_easy_apply_and_quick_apply_rows(self):
        """owner 2026-11-10: the agent uses LinkedIn Easy Apply and Handshake quick apply, 10 a day, 3 a run, newest first."""
        hours = lambda h: (NOW - datetime.timedelta(hours=h)).strftime("%Y-%m-%d %H:%M")
        p = self.plan([
            job("Newest Co", "https://www.linkedin.com/jobs/view/11/", notes="easy apply only", resume="a.pdf", posted_at=hours(1)),
            job("Quick Co", "https://app.joinhandshake.com/jobs/5", notes="quick apply only", resume="a.pdf", posted_at=hours(2)),
            job("No Link Co", "https://www.linkedin.com/jobs/view/12/", notes="easy apply only; employer link not found", resume="a.pdf", posted_at=hours(3)),
            job("Walled Easy Co", "https://www.linkedin.com/jobs/view/13/", status="Walled", notes="opened for you 2026-11-10 07:01: sign in and apply yourself (LinkedIn Easy Apply only); easy apply only", resume="a.pdf", posted_at=hours(50)),
            job("Three Days Co", "https://www.linkedin.com/jobs/view/14/", notes="easy apply only", resume="a.pdf", posted_at=hours(80)),
            job("No Resume Co", "https://www.linkedin.com/jobs/view/15/", notes="easy apply only", posted_at=hours(1)),
            job("Weak Co", "https://www.linkedin.com/jobs/view/16/", notes="easy apply only; weak match: shows 0 of 3", resume="a.pdf"),
            job("Sent Co", "https://www.linkedin.com/jobs/view/17/", notes="one-click sending 2026-11-10 10:00; easy apply only", resume="a.pdf"),
        ])
        self.assertEqual([x["company"] for x in p["to_one_click"]], ["Newest Co", "Quick Co", "Weak Co"])
        self.assertEqual(p["to_one_click"][1]["site"], "handshake")
        self.assertEqual(p["to_one_click"][0]["resume"], "a.pdf")
        why = p["skipped"]
        self.assertIn("3-per-run", why["https://www.linkedin.com/jobs/view/13"])
        self.assertIn("older than 72 h", why["https://www.linkedin.com/jobs/view/14"])
        self.assertIn("https://www.linkedin.com/jobs/view/15", p["to_match"])
        self.assertIn("3-per-run", why["https://www.linkedin.com/jobs/view/12"])
        self.assertIn("his to resolve", why["https://www.linkedin.com/jobs/view/17"])
        self.assertEqual(p["to_fill"], [])

    def test_one_click_daily_cap_and_company_limit(self):
        today = NOW.strftime("%Y-%m-%d")
        p = self.plan([
            job("Done Co", "https://www.linkedin.com/jobs/view/21/", status="Applied", notes=f"one-click applied {today} 09:07 (LinkedIn Easy Apply)", resume="a.pdf"),
            job("Busy Co", "https://www.linkedin.com/jobs/view/22/", title="Backend Engineer", status="Applied", notes=f"one-click applied {today} 09:07", resume="a.pdf"),
            job("Busy Co", "https://jobs.lever.co/busy/1", title="Data Engineer", status="Awaiting submit", notes=f"filled {today} 10:07, tab open", resume="a.pdf"),
            job("Busy Co", "https://www.linkedin.com/jobs/view/23/", title="Frontend Engineer", notes="easy apply only", resume="a.pdf"),
            job("Free Co", "https://www.linkedin.com/jobs/view/24/", notes="easy apply only", resume="a.pdf"),
            job("Late Co", "https://www.linkedin.com/jobs/view/25/", notes="easy apply only", resume="a.pdf"),
        ], one_click_per_day=3)
        self.assertEqual(p["one_click_today"], 2)
        self.assertEqual(p["one_click_left_today"], 1)
        self.assertEqual(len(p["to_one_click"]), 1)
        self.assertIn("2 applications today", p["skipped"]["https://www.linkedin.com/jobs/view/23"])

    def test_one_click_cap_counts_sent_applications_not_tries(self):
        """owner 2026-11-12: stopped or skipped tries used up the cap (8 tries, 1 sent). Only sent ones count."""
        today = NOW.strftime("%Y-%m-%d")
        p = self.plan([
            job("Stopped Co", "https://www.linkedin.com/jobs/view/31/", status="Awaiting submit", notes=f"one-click stopped {today} 09:34: needs you: x; easy apply only", resume="a.pdf"),
            job("Closed Co", "https://www.linkedin.com/jobs/view/32/", status="Not eligible", notes=f"one-click starting {today} 08:08; easy apply only", resume="a.pdf"),
            job("Sent Co", "https://www.linkedin.com/jobs/view/33/", status="Applied", notes=f"one-click applied {today} 07:30 (LinkedIn Easy Apply)", resume="a.pdf"),
            job("Next Co", "https://www.linkedin.com/jobs/view/34/", notes="easy apply only", resume="a.pdf"),
        ], one_click_per_day=3)
        self.assertEqual(p["one_click_today"], 1)
        self.assertEqual(p["one_click_left_today"], 2)
        self.assertEqual([x["company"] for x in p["to_one_click"]], ["Next Co"])

    def test_signed_in_sites_are_filled_not_opened(self):
        """step 3 (2026-11-10): once he signs in on a site, its jobs go to FILL with signed_in true."""
        hours = lambda h: (NOW - datetime.timedelta(hours=h)).strftime("%Y-%m-%d %H:%M")
        ss = "https://exampled.wd1.myworkdayjobs.com/Global/job/Quincy/Junior-Platform-Engineer_R-1"
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, TRACKER_DB=os.path.join(d, "t.sqlite"))
            run = lambda *a: subprocess.run([sys.executable, "-I", *a], capture_output=True, text=True, env=env)
            T = os.path.join(HERE, "tracker.py")
            run(T, "init")
            jobs = [
                job("Sample Bank", ss, status="Walled", notes="opened for you 2026-11-09 13:34: sign in and apply yourself", resume="a.pdf", posted_at=hours(50)),
                job("Sample Bank", "https://exampled.wd1.myworkdayjobs.com/Global/job/Example-City/Data-Engineer_R-2", title="Data Engineer", status="Matched", resume="a.pdf", notes="shows 3 of 3 must-haves"),
                job("Sample Bank", "https://exampled.wd1.myworkdayjobs.com/Global/job/Example-City/Old_R-3", title="Old Engineer", status="Walled", notes="opened for you", resume="a.pdf", posted_at=hours(80)),
                job("Sample Motors", "https://examplee.wd503.myworkdayjobs.com/tmna/job/Plano/SWE_1", status="Walled", notes="opened for you", resume="a.pdf"),
                job("Sample Health", "https://examplef.wd1.myworkdayjobs.com/Sample Health/job/x/AI_1", status="Matched", resume="a.pdf", notes="shows 2 of 2 must-haves"),
                job("Amazon", "https://www.amazon.jobs/en/jobs/1/sde", status="Walled", notes="needs you: account required", resume="a.pdf"),
                job("Captcha Co", "https://account.amazon.jobs/x/2/apply", status="Walled", notes="needs you: reCAPTCHA on form", resume="a.pdf"),
                job("Again Co", "https://careers-again.icims.com/jobs/9/job", status="Walled", notes="opened for you", resume="a.pdf"),
            ]
            jf, sf = os.path.join(d, "jobs.json"), os.path.join(d, "settings.json")
            json.dump(jobs, open(jf, "w"))
            json.dump({"fill_mode": "hourly", "max_posting_age_hours": 24}, open(sf, "w"))
            self.assertEqual(run(T, "upsert-job", jf).returncode, 0)
            self.assertEqual(run(T, "portal", ss, "signed-up", "test").returncode, 0)
            run(T, "portal", "https://careers-again.icims.com/jobs/9/job", "signed-up")
            # the agent met a sign-in page there after he signed up: a later time than the sign-up
            con = __import__("sqlite3").connect(env["TRACKER_DB"])
            con.execute("UPDATE portals SET signed_up_at='2026-01-01 00:00', wall_at='2026-01-02 00:00' WHERE portal='careers-again.icims.com'")
            con.commit(); con.close()
            os.makedirs(os.path.join(d, "scratch"))
            json.dump({"at": NOW.strftime("%Y-%m-%d %H:%M"), "open_job_keys": [], "portal_tabs": ["amazon.jobs"]}, open(os.path.join(d, "scratch", "open-tabs.json"), "w"))
            out = run(os.path.join(HERE, "plan.py"), "--hour", str(NOW.hour), "--settings", sf, "--state", os.path.join(d, "none.json"))
            self.assertEqual(out.returncode, 0, out.stderr)
            p = json.loads(out.stdout)
        fill = {x["company"] + "|" + x["title"]: x for x in p["to_fill"]}
        self.assertIn("Sample Bank|Software Engineer", fill)  # Walled, 50 h old: his sign-in is the go-ahead
        self.assertTrue(fill["Sample Bank|Software Engineer"]["signed_in"])
        self.assertEqual(fill["Sample Bank|Software Engineer"]["portal"], "exampled.wd1.myworkdayjobs.com")
        self.assertIn("Sample Bank|Data Engineer", fill)  # a new job on the same site
        self.assertIn("Amazon|Software Engineer", fill)  # he has an amazon.jobs application page open
        self.assertNotIn("Sample Motors|Software Engineer", fill)
        self.assertNotIn("Captcha Co|Software Engineer", fill)
        self.assertNotIn("Again Co|Software Engineer", fill)  # signed out since: his to sign in again
        self.assertIn("older than 72 h", p["skipped"]["https://exampled.wd1.myworkdayjobs.com/global/job/example-city/old_r-3"])
        self.assertEqual([x["company"] for x in p["walled_to_open"]], ["Sample Health"])
        self.assertIn("amazon.jobs", p["signed_in_sites"])

    def test_bytedance_tiktok_slots_go_to_85_plus_and_only_submits_count(self):
        """Exercise the configured application policy."""
        p = self.plan([
            job("Example Studio", "https://careers.studio.example.invalid/1", title="Old Opened", status="Stale", notes="opened for you 2026-11-05"),
            job("Example Studio", "https://careers.studio.example.invalid/2", title="Old Opened Too", status="Stale", notes="opened for you 2026-11-05"),
            job("Example Media", "https://careers.media.example.invalid/a", title="ML Engineer Graduate", score=96),
            job("Example Media", "https://careers.media.example.invalid/b", title="Backend Engineer Graduate", score=80),
            job("Example Independent Ventures", "https://www.linkedin.com/jobs/view/1/", title="Software Engineer, AI Data",
                status="Matched", resume="a.pdf", notes="easy apply only"),
        ])
        self.assertIn("ML Engineer Graduate", [x["title"] for x in p["walled_to_open"]])
        self.assertIn("scoring 85+", p["skipped"]["https://careers.media.example.invalid/b"])
        self.assertIn("Software Engineer, AI Data", [x["title"] for x in p["to_one_click"]])

    def test_same_job_with_a_differently_spelled_title_counts_as_applied(self):
        """Equivalent job titles from different sources share one history entry."""
        p = self.plan([
            job("Sample Fund Services", "https://app.joinhandshake.com/jobs/91000207", title="Junior Platform Developer", status="Applied"),
            job("Sample Fund Services", "https://jobs.lever.co/samplefund/1", title="Jr Platform Developer", status="Matched", resume="a.pdf", notes="shows 4 of 4 must-haves"),
        ])
        self.assertEqual(p["to_fill"], [])
        self.assertEqual(p["counts"]["jobs"], 1)  # the second posting is folded into the Applied row: never applied again


if __name__ == "__main__":
    unittest.main()
