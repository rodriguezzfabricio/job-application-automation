"""Quota admission and settlement against synthetic Codex meters."""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from job_runtime import pace

NOW = 1_800_000_000
WEEK = 7 * 86400


class WeeklyMeter(unittest.TestCase):
    def test_weekly_only_primary_drives_pace(self):
        limits = {'primary': {'usedPercent': 97, 'windowDurationMins': 10080, 'resetsAt': NOW + WEEK},
                  'secondary': None}
        meters = pace._meters(None, limits)
        self.assertNotIn('5h', meters)
        self.assertEqual(meters['week']['used'], 97)
        self.assertEqual(pace.decide(meters, NOW)['mode'], 'skip')

    def test_older_two_windows_use_duration_instead_of_order(self):
        limits = {'secondary': {'used_percent': 40, 'window_minutes': 300, 'resets_at': NOW + 60},
                  'primary': {'used_percent': 14, 'window_minutes': 10080, 'resets_at': NOW + WEEK}}
        meters = pace._meters(None, limits)
        self.assertEqual(meters['week']['used'], 14)
        self.assertEqual(meters['5h']['used'], 40)


class QuotaTests(unittest.TestCase):
    def setUp(self):
        from job_runtime.quota import Quota, QuotaUnavailable
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = NOW
        self.reset = NOW + WEEK
        self.used = 11
        self.account = 'fixture'
        self.Quota, self.denied = Quota, QuotaUnavailable
        self.root = Path(self.tmp.name)
        self.quota = self.ledger()

    def ledger(self):
        return self.Quota(self.root, meter=self.meter, clock=lambda: self.now)

    def test_shared_jump_defers_optional_work_but_keeps_ready_forms_funded(self):
        rid=self.quota.begin('application',budget=2,startup=1)
        self.used+=4
        with self.assertRaises(self.denied):
            self.quota.check(rid)
        self.assertTrue(self.quota.check_ready(rid)['ready_only'])
        self.assertEqual(self.quota.finish(rid)['charged'],5)

    def test_ready_continuation_stops_at_combined_cap_and_provider_headroom(self):
        rid=self.quota.begin('application',budget=2,startup=1)
        reading=self.meter()
        reading['5h']=dict(used=99,resets_at=self.now+1800,window_min=300)
        with self.assertRaises(self.denied):
            self.quota.check_ready(rid,reading=reading)
        self.used+=24
        with self.assertRaisesRegex(self.denied,'25-point'):
            self.quota.check_ready(rid)

    def test_account_transfer_preserves_charges_and_gets_a_new_daily_baseline(self):
        rid = self.quota.begin('benchmark',budget=1)
        self.quota.finish(rid)
        self.now += 10
        self.account = 'next-account'
        self.used = 20
        from unittest.mock import patch
        reading = dict(self.meter(),account_id_verified=True)
        with patch.object(self.quota,'meter',return_value=reading):
            moved = self.quota.rebind('fixture',self.account)
        self.assertEqual(moved['already_committed'],1)
        self.assertEqual(self.quota.pace_reading(reading)['day_start']['used'],20)
        with self.quota._transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(DISTINCT account) FROM account_meter_day').fetchone()[0],2)

    def meter(self):
        return {'account': self.account, 'sampled_at': self.now,
                'week': {'used': self.used, 'window_min': 10080, 'resets_at': self.reset}}

    def test_benchmark_has_a_nested_independent_cap(self):
        self.quota.begin('benchmark', budget=2)
        with self.assertRaisesRegex(self.denied, 'benchmark'):
            self.ledger().begin('benchmark', budget=1)
        self.quota.begin('application', budget=23)
        with self.assertRaisesRegex(self.denied, 'combined'):
            self.ledger().begin('application', budget=1)

    def test_benchmark_cannot_exceed_combined_cap(self):
        self.quota.begin('application', budget=24)
        with self.assertRaisesRegex(self.denied, 'combined'):
            self.ledger().begin('benchmark', budget=2)

    def test_concurrent_admission_shares_durable_holds(self):
        def reserve(_):
            try:
                return self.ledger().begin('benchmark', budget=1)
            except self.denied:
                return None
        with ThreadPoolExecutor(6) as pool:
            accepted = [rid for rid in pool.map(reserve, range(6)) if rid]
        self.assertEqual(len(accepted), 2)
        self.assertNotEqual(*accepted)

    def test_natural_reset_releases_old_holds_but_early_reset_does_not(self):
        rid = self.quota.begin('benchmark', budget=2)
        self.quota.suspend(rid)
        self.reset += 120
        self.used = 0
        with self.assertRaises(self.denied):
            self.quota.resume(rid)
        with self.assertRaises(self.denied):
            self.quota.begin('benchmark', budget=1)
        self.now = self.reset + 1
        self.reset = self.now + WEEK
        self.assertTrue(self.quota.begin('benchmark', budget=2))

    def test_zero_visible_delta_remains_charged_after_failure(self):
        rid = self.quota.begin('benchmark', budget=2)
        result = self.ledger().finish(rid)
        self.assertEqual(result['charged'], 2)
        self.assertEqual(result['observed_delta'], 0)
        with self.assertRaises(self.denied):
            self.ledger().begin('benchmark', budget=1)

    def test_missing_stale_or_changed_account_fails_closed(self):
        with self.assertRaises(self.denied):
            self.Quota(self.root, meter=lambda: {}, clock=lambda: self.now).begin('application')
        stale = self.meter()
        self.now += 1000
        with self.assertRaises(self.denied):
            self.Quota(self.root, meter=lambda: stale, clock=lambda: self.now).begin('application')
        rid = self.quota.begin('application')
        self.account = 'another'
        with self.assertRaises(self.denied):
            self.quota.resume(rid)
        with self.assertRaises(self.denied):
            self.quota.begin('application')

    def test_approved_owner_reuses_one_reservation_and_startup(self):
        rid = self.quota.begin('application', budget=2, startup=1, owner='worker-turn')
        self.assertEqual(self.ledger().begin('application', budget=2, startup=1, owner='worker-turn'), rid)
        self.assertEqual(self.ledger().check(rid)['remaining'], 1)
        self.used += 1
        with self.assertRaisesRegex(self.denied, 'allowance is used'):
            self.ledger().check(rid)
        self.assertEqual(self.ledger().finish(rid)['charged'], 2)

    def test_checkpoint_overrun_survives_a_settlement_meter_failure(self):
        rid = self.quota.begin('benchmark', budget=1)
        self.used += 3
        with self.assertRaises(self.denied):
            self.ledger().check(rid)
        broken = self.Quota(self.root, meter=lambda: {}, clock=lambda: self.now)
        settlement = broken.finish(rid)
        self.assertEqual(settlement['charged'], 3)
        self.assertEqual(settlement['status'], 'uncertain')
        self.assertIsNone(settlement['observed_delta'])

    def test_suspended_reservation_keeps_its_hold(self):
        rid = self.quota.begin('benchmark', budget=2)
        self.quota.suspend(rid)
        with self.assertRaises(self.denied):
            self.ledger().check(rid)
        with self.assertRaises(self.denied):
            self.ledger().begin('benchmark')
        self.assertEqual(self.ledger().resume(rid)['remaining'], 2)
        with self.assertRaises(self.denied):
            self.ledger().resume(rid)


class AppServerFixture(unittest.TestCase):
    def test_reads_current_workspace_and_weekly_window_over_stdio(self):
        import sys
        from job_runtime.usage import account_fingerprint, read_live_meters
        fixture = '''
import json
import sys
import time
for line in sys.stdin:
    req = json.loads(line)
    method = req['method']
    if method == 'initialize':
        result = {}
    elif method == 'initialized':
        continue
    elif method == 'account/read':
        assert req['params']['refreshToken'] is False
        result = {'account': {'type': 'chatgpt', 'email': 'fixture@example.test'}, 'userId': 'synthetic-user'}
    elif method == 'account/rateLimits/read':
        result = {'accountId': 'synthetic-workspace', 'ordinaryUsageAllowed': True,
                  'rateLimits': {'limitId': 'irrelevant'}, 'rateLimitsByLimitId': {'codex': {
                      'limitId': 'codex', 'secondary': None,
                      'primary': {'usedPercent': 17, 'windowDurationMins': 10080, 'resetsAt': int(time.time()) + 86400}}}}
    else:
        raise RuntimeError(method)
    print(json.dumps({'id': req['id'], 'result': result}), flush=True)
'''
        reading = read_live_meters([sys.executable, '-c', fixture], timeout=3)
        self.assertEqual(reading['account'], account_fingerprint('synthetic-workspace'))
        self.assertEqual(reading['week']['used'], 17)
        self.assertNotIn('5h', reading)
        denied = fixture.replace("'ordinaryUsageAllowed': True", "'ordinaryUsageAllowed': False")
        self.assertEqual(read_live_meters([sys.executable, '-c', denied], timeout=3), {})


class ScorecardFixture(unittest.TestCase):
    def test_weekly_stats_dont_use_short_window_fit_and_cross_reset_is_unknown(self):
        import json
        from codex.eval.scorecard import log_stats

        count = {'input_tokens': 100_000, 'cached_input_tokens': 1_000}
        def event(used, resets):
            return json.dumps({'payload': {'type': 'token_count',
                  'rate_limits': {'limit_id': 'codex', 'primary': {
                      'used_percent': used, 'window_minutes': 10080, 'resets_at': resets}, 'secondary': None},
                  'info': {'last_token_usage': count, 'total_token_usage': count}}})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'meter.jsonl'
            path.write_text('\n'.join([event(18, NOW), event(19, NOW)]))
            stats = log_stats(path)
            self.assertEqual(stats['pct_week'], 1)
            self.assertIsNone(stats['pct_5h'])
            self.assertIsNone(stats['model_pct'])
            path.write_text('\n'.join([event(18, NOW), event(19, NOW + WEEK)]))
            self.assertIsNone(log_stats(path)['pct_week'])
