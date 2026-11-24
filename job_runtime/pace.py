"""Usage pace gate: spread ONE ChatGPT Plus account's WEEKLY allowance over the week (owner 2026-11-13: keep the
application volume; one account).

Why (2026-11-13 session logs, the local usage report):
  - The 5-hour meter tracks input tokens: about 40 points per million uncached input tokens plus 1.2-1.3 per
    million cached ones (R2 0.995). Every tool call re-sends the whole context, and every run start pays ~0.9%
    for its uncached prompt, so runs that do little are the most expensive per application.
  - The weekly meter rose 15-18 points per full 5-hour window, so a week holds about 5.5-6 windows: about 14
    weekly points a day if spread evenly. On 11-13 the runs used ~45 weekly points in one day (two server-side
    resets that day hid it); at that pace the week is gone in 2-3 days and every later run is dead.
  - The 5-hour meter hit 100% at 16:19 and 18:27; a run started near it died mid-run holding the lease.

So the weekly meter decides first: today's share = what is left of the week / days until it resets (fixed at the
day's first reading), released over the 06:00-24:00 operating day with 2 hours of headroom. The 5-hour meter is
only the short-term ceiling. Answers:
  run   do the whole stage list;
  lean  apply first: discovery stages are deferred (their queues stay in the database); ready Easy Apply jobs,
        forms and cheap matches are still done; the stop hook stops pushing card reading;
  skip  start nothing (the run costs ~1% instead of a run that wastes its fixed cost or dies at the limit):
        today's share is used up, the 5-hour meter is nearly full, or a lean run would find nothing ready.
Pace advice is independent of the durable spending caps in quota.py. Historical session logs can show
usage trends and context size. Live quota admission requires an authenticated app-server reading.
"""
import datetime as dt
import glob
import json
import math
import os
import re
import time
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
SESSIONS = os.path.expanduser('~/.codex/sessions')

WINDOW_5H_MIN = 300
WINDOW_WEEK_MIN = 10080
HEADROOM_5H_MIN = 75          # may run up to 25 points ahead of an even 5-hour spend before going lean
LEAN_5H_PCT = 80              # no room left for a full run (productive runs cost 10-34%): apply only
SKIP_5H_PCT = 92              # a run started here stops at the limit mid-run (16:18 at 97%, 18:18 at 83% -> 100%)
OPERATING_HOURS = (6, 24)     # ET: runs only happen 06:00-23:59
WEEK_HEADROOM_H = 2.0         # operating hours of the day's weekly share that may be spent early
SKIP_WEEK_PCT = 97
CONTEXT_WRAP_TOKENS = 180000  # past this context a call costs >0.2% of 5 h; wrap up, the next run starts small

SAFE_ID = re.compile(r'^[A-Za-z0-9-]{6,80}$')


def _lines(path, nbytes=600000, head=False):
    try:
        with open(path, 'rb') as fh:
            if not head:
                fh.seek(max(0, os.path.getsize(path) - nbytes))
            return fh.read(nbytes).decode('utf-8', 'ignore').splitlines()
    except OSError:
        return []


def _rate(line):
    if '"token_count"' not in line or '"rate_limits"' not in line:
        return None, None
    try:
        d = json.loads(line)
    except ValueError:
        return None, None
    p = d.get('payload') or {}
    rl = p.get('rate_limits') if p.get('type') == 'token_count' else None
    if isinstance(rl, dict) and rl.get('limit_id') in (None, 'codex'):
        return d.get('timestamp'), rl
    return None, None


def _meters(at, rl):
    """Decode both transcript and app-server snapshots by actual window duration."""
    out = {'at': at}
    names = {WINDOW_5H_MIN: '5h', WINDOW_WEEK_MIN: 'week'}
    for key in ('primary', 'secondary'):
        raw = rl.get(key)
        if not isinstance(raw, dict):
            continue
        try:
            window = float(raw.get('windowDurationMins', raw.get('window_minutes')))
            name = names.get(window)
            used = float(raw.get('usedPercent', raw.get('used_percent')))
            reset = float(raw.get('resetsAt', raw.get('resets_at')))
        except (ValueError, TypeError):
            continue
        if name and 0 <= used <= 100 and math.isfinite(reset):
            out[name] = {'used': used, 'resets_at': reset, 'window_min': int(window)}
    return out


def same_week(a, b):
    """Both readings are from the same weekly window (no server-side reset in between)."""
    if not a or not b or not a.get('resets_at') or not b.get('resets_at'):
        return False
    return float(a['resets_at']) == float(b['resets_at'])


def day_start(week, sessions=SESSIONS, now=None):
    """The first reading of today (ET date) from the same weekly window: {'used', 'at'} or None."""
    if not week:
        return None
    today = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ET)
    files = sorted(glob.glob(os.path.join(sessions, today.strftime('%Y'), today.strftime('%m'), today.strftime('%d'), 'rollout-*.jsonl')))
    for f in files:
        for line in _lines(f, 400000, head=True):
            at, rl = _rate(line)
            if rl:
                m = _meters(at, rl)
                if same_week(m.get('week'), week):
                    return {'used': m['week']['used'], 'at': at}
                break                                  # one record per file is enough to judge that file
    return None


def read_meters(path=None, sessions=SESSIONS, now=None):
    """{'5h', 'week', 'day_start', 'at', 'file'} from the newest Codex session log with a rate-limit record; {} if none.
    The meters belong to the signed-in account, so any session's newest record will do."""
    files = [path] if path else sorted(glob.glob(os.path.join(sessions, '*', '*', '*', 'rollout-*.jsonl')),
                                       key=os.path.getmtime, reverse=True)[:3]
    for f in files:
        for line in reversed(_lines(f)):
            at, rl = _rate(line)
            if rl:
                out = _meters(at, rl)
                if not (out.get('week') or out.get('5h')):
                    continue
                out['file'] = f
                if out.get('week'):
                    out['day_start'] = day_start(out['week'], sessions, now)
                return out
    return {}


def session_file(session_id, sessions=SESSIONS):
    if not session_id or not SAFE_ID.match(str(session_id)):
        return None
    hits = glob.glob(os.path.join(sessions, '*', '*', '*', 'rollout-*%s.jsonl' % session_id))
    return max(hits, key=os.path.getmtime) if hits else None


def session_context(session_id, sessions=SESSIONS):
    """Input tokens of this session's newest model request: the context every further call re-sends."""
    f = session_file(session_id, sessions)
    if not f:
        return None
    for line in reversed(_lines(f, 300000)):
        if '"token_count"' not in line or '"last_token_usage"' not in line:
            continue
        try:
            info = (json.loads(line).get('payload') or {}).get('info') or {}
            return int(info['last_token_usage']['input_tokens'])
        except (ValueError, KeyError, TypeError):
            continue
    return None


def _ts(now):
    if now is None:
        return time.time()
    return now.timestamp() if isinstance(now, dt.datetime) else float(now)


def five_hour(m, now_ts):
    """used and even-spend budget (percent) of the 5-hour meter; a window that has reset counts as unused."""
    used = float(m.get('used') or 0)
    if not m.get('resets_at'):
        return {'used': used, 'budget': None}
    window = float(m.get('window_min') or WINDOW_5H_MIN)
    left = (float(m['resets_at']) - now_ts) / 60
    if left <= 0:
        return {'used': 0.0, 'budget': round(100.0 * HEADROOM_5H_MIN / window, 1)}
    return {'used': used, 'budget': round(min(100.0, 100.0 * (window - left + HEADROOM_5H_MIN) / window), 1),
            'resets': dt.datetime.fromtimestamp(float(m['resets_at']), ET).strftime('%a %H:%M')}


def weekly(m, start, now_ts):
    """Today's share of the weekly allowance: per_day (points), allowed so far, spent so far."""
    used = float(m.get('used') or 0)
    if not m.get('resets_at'):
        return {'used': used, 'per_day': None}
    left_days = max((float(m['resets_at']) - now_ts) / 86400, 0.0)
    if left_days <= 0:                                          # reading older than the window: it has reset
        used, left_days, start = 0.0, 7.0, None
    begin_used, begin_ts = used, now_ts
    if start and start.get('used') is not None and float(start['used']) <= used:
        begin_used = float(start['used'])
        try:
            begin_ts = dt.datetime.fromisoformat(str(start['at']).replace('Z', '+00:00')).timestamp()
        except ValueError:
            begin_ts = now_ts
    days_at_start = left_days + max(0.0, now_ts - begin_ts) / 86400
    per_day = (100.0 - begin_used) / max(days_at_start, 1.0)       # less than a day left: today may use it all
    local = dt.datetime.fromtimestamp(now_ts, ET)
    lo, hi = OPERATING_HOURS
    frac = min(1.0, max(0.0, (local.hour + local.minute / 60 - lo) / (hi - lo)))
    allowed = per_day * min(1.0, frac + WEEK_HEADROOM_H / (hi - lo))
    return {'used': used, 'per_day': round(per_day, 1), 'allowed_now': round(allowed, 1), 'spent_today': round(used - begin_used, 1),
            'resets': dt.datetime.fromtimestamp(float(m['resets_at']), ET).strftime('%a %H:%M')}


def decide(meters, now=None, ready=None):
    """'run' | 'lean' | 'skip', why, and both meters. ready = jobs a lean run could still apply to or match
    (None = unknown); a lean run with nothing ready is a skip."""
    now_ts = _ts(now)
    out = {'mode': 'run', 'why': 'no usage record: run normally'}
    if not meters or not (meters.get('5h') or meters.get('week')):
        return out
    h = five_hour(meters['5h'], now_ts) if meters.get('5h') else None
    w = weekly(meters['week'], meters.get('day_start'), now_ts) if meters.get('week') else None
    out.update({'5h': h, 'week': w})
    lean = ''
    if h and h['used'] >= SKIP_5H_PCT:
        out.update(mode='skip', why='5-hour meter at %.0f%%: a run started now would stop at the limit mid-run' % h['used'])
        return out
    if w and w['used'] >= SKIP_WEEK_PCT:
        out.update(mode='skip', why='weekly meter at %.0f%%' % w['used'])
        return out
    if ready == 0:
        out.update(mode='skip', why='no actionable openings in the application queue')
        return out
    if w and w.get('per_day') is not None:
        if w['spent_today'] >= w['per_day']:
            out.update(mode='lean' if ready else 'skip', why="today's share of the weekly allowance (%.1f points) is used (%.1f)" % (w['per_day'], w['spent_today']))
            return out
        if w['spent_today'] > w['allowed_now']:
            lean = 'weekly meter ahead of pace: %.1f of %.1f points allowed by now (today %.1f)' % (w['spent_today'], w['allowed_now'], w['per_day'])
    if not lean and h and h['used'] >= LEAN_5H_PCT:
        lean = '5-hour meter at %.0f%%: no room for a full run' % h['used']
    if not lean and h and h.get('budget') is not None and h['used'] > h['budget']:
        lean = '5-hour meter %.0f%% is ahead of an even spend (%.0f%%)' % (h['used'], h['budget'])
    if lean:
        if ready == 0:
            out.update(mode='skip', why=lean + '; nothing is ready to apply or match')
        else:
            out.update(mode='lean', why=lean)
    else:
        out['why'] = 'within pace'
    return out


def server_reset(old, new):
    """A server-side reset between two readings (11-13 17:18 and 18:41 ET: weekly 20% -> 0% and 16% -> 0% with new
    7-day reset times on the same account): the weekly reset time moved or the weekly meter went down."""
    ow, nw = (old or {}).get('week') or {}, (new or {}).get('week') or {}
    if not ow or not nw:
        return False
    return not same_week(ow, nw) or float(nw.get('used') or 0) < float(ow.get('used') or 0) - 1


def live_mode(stored, live_meters=None, now=None):
    """Re-judge a running run. The newest reading decides on its own; the mode the run began with is kept as a
    floor unless a server-side reset came in between. No live reading: the stored mode."""
    order = {'run': 0, 'lean': 1, 'skip': 2}
    base = (stored or {}).get('mode', 'run')
    if not live_meters or not (live_meters.get('5h') or live_meters.get('week')):
        return base
    mode = decide(live_meters, now, ready=1)['mode']  # This run already has an admitted batch.
    if stored and not server_reset(stored.get('meters'), live_meters):
        mode = max(mode, base, key=order.get)
    return mode


def summary(d):
    """Short lines for printing."""
    h, w = d.get('5h'), d.get('week')
    out = {'mode': d['mode'], 'why': d['why']}
    for key in ('ready_openings','qualification_reviews'):
        if key in d:
            out[key] = d[key]
    if h:
        out['5h'] = '%.0f%% used%s' % (h['used'], '' if h.get('budget') is None else ', even-spend budget %.0f%%%s' % (
            h['budget'], ', resets ' + h['resets'] if h.get('resets') else ''))
    if w:
        out['week'] = '%.0f%% used%s' % (w['used'], '' if w.get('per_day') is None else
                                         '; today %.1f of %.1f points spent (%.1f allowed by now), resets %s' % (
                                             w['spent_today'], w['per_day'], w['allowed_now'], w['resets']))
    return out
