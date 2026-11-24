"""Only the owner's 'fill now' jumps the fill queue; a tab-sweep 'Ready to fill' does not.

Regression 2026-11-13: three Nov-11 rows the tab sweep had set to 'Ready to fill'
took every fill slot in every Codex run, were refused by the fill stage as too old,
and fresh jobs were never filled.
"""
import datetime as dt
import unittest
from test_runtime import RuntimeTests
from job_runtime import adapters
from job_runtime.planner import plan
from job_runtime.policy import owner_go


class OwnerGoTests(RuntimeTests):
    def old_system_ready(self, company, suffix):
        old = (self.at - dt.timedelta(hours=48)).isoformat()
        return self.job(company, suffix, status='Ready to fill', posted_at=old)

    def test_system_ready_rows_do_not_hold_fill_slots(self):
        stale = [self.old_system_ready(c, c) for c in ('Jamf', 'R3', 'AbbVie')]
        queues = plan(self.s, self.policy, 'FULL')
        self.assertEqual(queues['to_fill'], [self.key])
        for key in stale:
            self.assertEqual(queues['skipped'][key], 'posting outside freshness window')

    def test_today_fill_now_is_owner_go_ahead_and_goes_first(self):
        old = self.old_system_ready('Owner Pick', 'pick')
        self.packet['commands']['Today'].append(['Press Submit', 'fill now', old])
        adapters.import_commands(self.s, self.packet, self.owner, self.token)
        self.assertTrue(owner_go(self.s.job(old)))
        queues = plan(self.s, self.policy, 'FULL')
        self.assertEqual(queues['to_fill'][:2], [old, self.key])

    def test_jobs_your_call_fill_now_is_owner_go_ahead(self):
        old = self.old_system_ready('Owner Call', 'call')
        self.packet['commands']['Jobs'].append(['fill now', old])
        adapters.import_commands(self.s, self.packet, self.owner, self.token)
        self.assertEqual(plan(self.s, self.policy, 'FULL')['to_fill'][0], old)

    def test_recorded_fill_blockers_free_the_slot(self):
        visa = self.job('Sample Network', 'visa')
        uno = self.job('Uno Wallet', 'uno')
        affirm = self.job('Example Lending', 'affirm', posted_at=(self.at - dt.timedelta(hours=20)).isoformat())
        adapters.defer(self.s, 'fill', 'Sample Network', {'reason': 'Create Account/Sign In step1of8'}, self.owner, self.token)
        adapters.defer(self.s, 'fill', uno, {'reason': 'Required other document'}, self.owner, self.token)
        queues = plan(self.s, self.policy, 'FAST')
        self.assertEqual(queues['to_fill'], [self.key, affirm])
        self.assertEqual(queues['skipped'][visa], 'fill blocked: Create Account/Sign In step1of8')
        self.assertIn('fill blocked', queues['skipped'][uno])

    def test_fill_blocker_expires_after_a_day(self):
        visa = self.job('Sample Network', 'visa')
        adapters.defer(self.s, 'fill', 'Sample Network', {'reason': 'wall'}, self.owner, self.token)
        self.at += dt.timedelta(hours=25)
        self.s.con.execute('UPDATE jobs SET posted_at=? WHERE job_key=?', (self.at.isoformat(), visa))
        self.assertIn(visa, plan(self.s, self.policy, 'FULL')['to_fill'])

    def test_browser_glitch_is_retried_after_thirty_minutes(self):
        affirm = self.job('Example Lending', 'affirm')
        adapters.defer(self.s, 'fill', affirm, {'reason': 'CUA filechooser timed out after10000ms'}, self.owner, self.token)
        self.assertIn('fill blocked', plan(self.s, self.policy, 'FAST')['skipped'][affirm])
        self.at += dt.timedelta(minutes=31)
        self.assertIn(affirm, plan(self.s, self.policy, 'FAST')['to_fill'])

    def test_owner_go_ahead_overrides_a_fill_blocker(self):
        visa = self.job('Sample Network', 'visa')
        self.s.con.execute("UPDATE jobs SET your_call='fill now' WHERE job_key=?", (visa,))
        adapters.defer(self.s, 'fill', 'Sample Network', {'reason': 'wall'}, self.owner, self.token)
        self.assertEqual(plan(self.s, self.policy, 'FAST')['to_fill'][0], visa)

    def test_status_alone_is_not_owner_go_ahead(self):
        self.assertFalse(owner_go({'status': 'Ready to fill', 'your_call': ''}))
        self.assertTrue(owner_go({'status': 'Matched', 'your_call': 'Fill now '}))


class PolicySnapshotTests(RuntimeTests):
    def test_planning_tolerates_an_old_snapshot_but_submit_does_not(self):
        import datetime as dt2
        from job_runtime.store import Denied
        self.at += dt2.timedelta(minutes=20)          # a long run: the sheet read is 20 minutes old
        self.assertEqual(plan(self.s, self.s.policy_snapshot(), 'FAST')['mode'], 'FAST')
        with self.assertRaises(Denied):
            self.s.latest_policy()                      # final submission still needs a fresh read
        self.at += dt2.timedelta(hours=1)
        with self.assertRaises(Denied):
            self.s.policy_snapshot()                    # but not an old one from another run


class PostedTimeTests(unittest.TestCase):
    def test_claude_reading_of_dates(self):
        import datetime as dt3
        from job_runtime.policy import posted_time, ET
        t = posted_time({'posted_at': '2026-11-12', 'found_at': '2026-11-13 09:00'})
        self.assertEqual((t.astimezone(ET).date().isoformat(), t.astimezone(ET).hour, t.astimezone(ET).minute), ('2026-11-12', 23, 59))
        self.assertEqual(posted_time({'posted_at': '', 'found_at': '2026-11-13 09:00'}).astimezone(ET).hour, 9)
        self.assertIsNone(posted_time({'posted_at': '', 'found_at': ''}))
        full = posted_time({'posted_at': '2026-11-12T08:15:00-05:00', 'found_at': '2026-11-13 09:00'})
        self.assertEqual(full.astimezone(ET).hour, 8)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite(OwnerGoTests(name) for name in OwnerGoTests.__dict__ if name.startswith('test_'))
    suite.addTests(PolicySnapshotTests(n) for n in PolicySnapshotTests.__dict__ if n.startswith('test_'))
    suite.addTests(loader.loadTestsFromTestCase(PostedTimeTests))
    return suite
