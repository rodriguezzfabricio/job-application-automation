"""Codex Stop hook logic: a job run may not end its turn while the database shows work left.

Why: the cheaper model
(gpt-6.1-terra) ended both of its test runs (18:40, 19:00) right after startup with "cycle incomplete,
resume next time" while 21 LinkedIn cards, an Easy Apply job and 3 forms were queued, and Sol runs also
ended 'partial' with work left. Prompt rules ("never end your turn while work remains") and finish()'s
refusal did not stop that, because the model can always just stop talking. Claude's coordinator kept
going until its queue was empty. Codex runs a Stop hook when the model tries to end its turn; a
{"decision": "block", "reason": ...} answer makes Codex continue the turn with `reason` as the next prompt.

Scope and limits (never trap a session):
  - only a session that owns a jr_runs row with status 'running' (owner = the Codex thread id);
  - never when the last message names a run-level blocker (usage limit, CAPTCHA, logged out, browser down)
    or the 5-hour ChatGPT allowance is at USAGE_STOP_PCT or more;
  - at most MAX_BLOCKS continuations per run, and past RUN_CAP_MINUTES, or once the session's context passes
    pace.CONTEXT_WRAP_TOKENS, only one wrap-up push (receipts, audit, mirror, finish) and then never again.
The hook reads the database and writes only its own state file (codex-runtime/runs/<run_id>/stop-hook.json).

Usage (2026-11-13 logs, the local usage report): every call re-sends the whole context,
so a run's cost grows with the square of its length: a context over 180k tokens wraps up even before the time
cap (runs start every 2 hours, so the 45-minute cap never overlaps the next run). A lean run (pace.py: allowance ahead of an even spend) is never pushed
to read more cards; it is still pushed to apply to ready jobs and to close its stages.
"""
import json

from . import pace
from .coordinator import RUN_BLOCKER, remaining_work, run_pace
from .policy import timestamp

MAX_BLOCKS = 8
RUN_CAP_MINUTES = {'FAST': 35, 'FULL': 45}     # schedule: one FULL run every 2 hours (automation.toml); context cap first
USAGE_STOP_PCT = 90

CARD_HOW = ("; per card: in the browser (js) tool open the card url in your tab, read it with the cards_js page_js and "
            "print {id, read:{description, apply, company, title, location, age}} (description up to 5000 chars); then "
            "in the exec tool run `codex/jr card_commit '{\"source\":\"%s\",\"id\":...,\"read\":{...}}'` with what you "
            "printed. The two tools do not share variables: copying the printed JSON is the way (Sol does this)")
NEXT = (
    ('linkedin cards to read', "read LinkedIn cards: `codex/jr cards_next '{\"source\":\"linkedin\"}'`" + CARD_HOW % 'linkedin'),
    ('handshake cards to read', "read Handshake cards: `codex/jr cards_next '{\"source\":\"handshake\"}'`" + CARD_HOW % 'handshake'),
    ('postings with full text to match', "match: `codex/jr match_batch_prepare '{\"limit\":8}'`, write the requirements file, "
                                         "`codex/jr match_batch_commit '{}'`"),
    ('Easy Apply / quick apply jobs ready', 'one_click: `codex/jr brief` lists the jobs; apply to each'),
    ('forms ready to fill', 'fill: `codex/jr brief` lists the forms; fill and submit each one you can'),
)


def _state_path(store, run_id):
    return store.path.parent / 'codex-runtime' / 'runs' / run_id / 'stop-hook.json'


def _load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {'blocks': 0, 'log': []}


def _save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1))


def stages_without_receipt(store, run):
    """Expected stages that have no succeeded/failed/deferred receipt yet, in run order."""
    done = {r['stage'] for r in store.con.execute(
        "SELECT stage FROM jr_actions WHERE run_id=? AND status<>'pending'", (run['id'],))}
    return [s for s in json.loads(run['expected']) if s not in done]


def continuation_funded(store, run_id):
    from .quota import Quota, QuotaUnavailable
    reservation = store.con.execute('SELECT value FROM jr_meta WHERE key=?', ('quota:' + run_id,)).fetchone()
    if not reservation:
        return False
    try:
        ledger=Quota(store.path.parent)
        if store.codex_production():
            ledger.check_ready(reservation[0])
        else:
            ledger.check(reservation[0])
    except QuotaUnavailable:
        return False
    return True


def decide(store, session_id, last_message='', usage_pct=None, context_tokens=None):
    """None = let the turn end. A dict = Codex Stop-hook output that continues the turn.
    context_tokens: the session's current context; None reads it from the session's own Codex log."""
    run = store.con.execute("SELECT * FROM jr_runs WHERE owner=? AND status='running' ORDER BY started DESC LIMIT 1",
                            (str(session_id or ''),)).fetchone()
    if not run:
        return None
    if RUN_BLOCKER.search(str(last_message or '')):
        return None
    if not continuation_funded(store, run['id']):
        return None
    if usage_pct is not None and usage_pct >= USAGE_STOP_PCT:
        return None
    path = _state_path(store, run['id'])
    state = _load(path)
    if state.get('blocks', 0) >= MAX_BLOCKS:
        return None
    # keep_going.py passes the live 5-hour reading; then the newest rate-limit record (both meters, reset times)
    # decides, and the run's starting mode is kept only while the account and weekly window are unchanged.
    live = pace.read_meters() if usage_pct is not None else None
    mode = pace.live_mode(run_pace(store, run['id']), live, store.clock())
    if mode == 'skip':
        return None
    if context_tokens is None:
        try:
            context_tokens = pace.session_context(session_id)
        except Exception:
            context_tokens = None
    started = timestamp(run['started'])
    elapsed = (store.clock() - started).total_seconds() / 60 if started else 0
    cap = RUN_CAP_MINUTES.get(run['mode'], 35)
    stages = stages_without_receipt(store, run)
    head = '[stop hook, automatic; not the owner] Do not end your turn: run %s (%s, %.0f of %d min%s) is still open. ' % (
        run['id'][:8], run['mode'], elapsed, cap, ', lean: usage pace' if mode == 'lean' else '')
    big = context_tokens is not None and context_tokens >= pace.CONTEXT_WRAP_TOKENS
    if elapsed >= cap or big:
        if state.get('wrap_up_sent'):
            return None
        state['wrap_up_sent'] = True
        why = ('Time is up' if elapsed >= cap else
               'This session\'s context is %dk tokens, so every further call costs over 0.23%% of the 5-hour allowance; '
               'the next run continues the queue with a small context' % (context_tokens // 1000))
        reason = (head + why + ': start no new job. Record a receipt for each stage still open (%s) with '
                  "`codex/jr stage <name> deferred '{\"refs\":[\"run time cap\"]}'`, run `codex/jr audit '{}'`, update the "
                  "mirror (stages/MIRROR.md), then `codex/jr finish '{}'`." % (', '.join(stages) or 'none'))
        kind = 'wrap_up' if elapsed >= cap else 'wrap_up_context'
    else:
        try:
            left = remaining_work(store, run['id'], lean=(mode == 'lean'))
        except Exception:                      # no fresh settings snapshot etc.: judge by stages alone
            left = {}
        if left:
            hint = next(h for name, h in NEXT if name in left) if any(name in left for name, _ in NEXT) else 'continue the stage loop'
            reason = (head + 'The database shows work left: %s. Next: %s. Stages without a receipt: %s. '
                      % ('; '.join('%d %s' % (n, k) for k, n in left.items()), hint, ', '.join(stages) or 'none'))
            kind = 'work_left'
        elif stages:
            reason = (head + 'No queued jobs, but these stages have no receipt: %s. Do them in order (codex/BRIEF.md), '
                      "then `codex/jr finish '{}'`. " % ', '.join(stages))
            kind = 'stages_left'
        else:
            reason = head + "Every stage has a receipt: call `codex/jr finish '{}'` now to close the run. "
            kind = 'finish'
        if mode == 'lean':
            reason += ('Usage pace is LEAN: read no more cards; record an open handshake/linkedin stage as deferred with '
                       "refs [\"usage pace\"], apply to ready jobs, then audit, mirror and finish. ")
        reason += ('A problem with ONE job (unapproved term, unsupported account flow, failed Easy Apply, missing '
                   'answer) is a per-job handoff: record it and go to the next job. A browser-tab error means open your own '
                   "tab with cua.createBrowserTab('1', url). Stop only for a run-level blocker (usage limit, CAPTCHA, "
                   "logged out, browser down): then call `codex/jr finish '{\"blocker\":\"<what>\"}'`.")
    state['blocks'] = state.get('blocks', 0) + 1
    state.setdefault('log', []).append({'at': store.now(), 'kind': kind, 'elapsed_min': round(elapsed, 1),
                                        'context_k': context_tokens // 1000 if context_tokens else None, 'pace': mode})
    _save(path, state)
    return {'decision': 'block', 'reason': reason}
