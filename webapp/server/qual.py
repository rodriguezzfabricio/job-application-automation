"""Qualification fairness queue (a2.md Q41) — pure scheduling logic, no I/O.

Job state is passed in and out explicitly: dict with keys
failures, qual_next_due, on_hold_until. Nothing is kept on self.
"""


class QualQueue:

    def __init__(self, shortlist_limit, backoff_base_s=900.0, backoff_cap_s=86400.0, hold_s=7 * 86400):
        self.shortlist_limit = shortlist_limit
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self.hold_s = hold_s

    def pick(self, jobs, states, now):
        """Eligible ids in next_due order (None first), ties by id, at most shortlist_limit."""
        eligible = []
        for job_id in jobs:
            state = states.get(job_id, _fresh_state())
            if _is_on_hold(state, now):
                continue
            due = state.get("qual_next_due")
            if due is None or due <= now:
                eligible.append(job_id)
        eligible.sort(key=lambda job_id: _due_key(states.get(job_id, _fresh_state()), job_id))
        return eligible[: self.shortlist_limit]

    def record_failure(self, job_id, state, now):
        """Strikes: 1 -> still eligible, 2 -> 2x backoff, 3 -> 4x capped, 4+ -> 7-day hold."""
        failures = state.get("failures", 0) + 1
        new_state = dict(state, failures=failures)
        if failures == 2:
            new_state["qual_next_due"] = now + 2 * self.backoff_base_s
        elif failures == 3:
            new_state["qual_next_due"] = now + min(4 * self.backoff_base_s, self.backoff_cap_s)
        elif failures >= 4:
            # Hold pins both the due time and the hold end so pick can't see the job.
            new_state["qual_next_due"] = now + self.hold_s
            new_state["on_hold_until"] = now + self.hold_s
        return new_state

    def record_success(self, job_id, state, now):
        return {"failures": 0, "qual_next_due": None, "on_hold_until": None}

    def retry_now(self, job_id, state, now):
        """Operator override: clear a deferral but keep strikes; never lifts an active hold."""
        if _is_on_hold(state, now):
            return dict(state)
        return dict(state, qual_next_due=None)

    def step_cycle(self, jobs, states, failing_ids, now, cycle_length_s):
        """One qualification cycle: pick, apply qualifier outcomes, advance the clock."""
        picked = self.pick(jobs, states, now)
        states = dict(states)
        failing = set(failing_ids)
        for job_id in picked:
            state = states.get(job_id, _fresh_state())
            if job_id in failing:
                states[job_id] = self.record_failure(job_id, state, now)
            else:
                states[job_id] = self.record_success(job_id, state, now)
        return picked, states, now + cycle_length_s


def _fresh_state():
    return {"failures": 0, "qual_next_due": None, "on_hold_until": None}


def _is_on_hold(state, now):
    until = state.get("on_hold_until")
    return until is not None and until > now


def _due_key(state, job_id):
    due = state.get("qual_next_due")
    # None sorts first: a job with no deferral is the most overdue.
    return (due if due is not None else float("-inf"), job_id)
