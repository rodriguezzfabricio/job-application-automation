#!/usr/bin/env python3
"""One control point for pausing and resuming the job automation (owner 2026-11-21).

Why this exists: on 2026-11-18 the owner paused 5 switches in 4 places. On 11-19 he said "Turn on all job
automations now", and that resume turned only 3 back on. The Job Dashboard still said paused = yes and the phone
alerts stayed off, so for 3 days every run found jobs, applied to none, and nothing told him.

This script owns the switches on the Mac and prints the others as a checklist. `status --sheet-paused`, called at
step 0 of every run, finds a half-paused state so the run can alert him about it.

Usage:
  jobctl.py status [--sheet-paused yes|no] [--json]   exit 0 = consistent, 1 = problems found
  jobctl.py pause                                      Mac switches off, then prints the remote steps
  jobctl.py resume                                     Mac switches on, then prints the remote steps

Pass --sheet-paused only from inside a run: it means "a run is happening, and the sheet says this".

Switches on the Mac (this script):
  - pause file  ~/Library/Application Support/job-watchdog/paused  (the watchdog stays silent while it exists)
  - launch agents local.jobtools.job-notify, local.jobtools.job-watchdog, local.jobtools.claude-keepalive
Remote switches (agent tools only, listed by REMOTE_STEPS below):
  - Claude desktop task job-desktop-hourly, cloud routine "Job discovery", Job Dashboard Settings!B7 paused
"""
import argparse, datetime, json, os, re, shutil, subprocess, sys, time

BOX = os.environ.get("JOBCTL_BOX") or os.path.expanduser("~/Library/Application Support/job-watchdog")
AGENTS_DIR = os.environ.get("JOBCTL_AGENTS_DIR") or os.path.expanduser("~/Library/LaunchAgents")
LAUNCHCTL = os.environ.get("JOBCTL_LAUNCHCTL", "/bin/launchctl")
LOCK = os.environ.get("JOBCTL_LOCK") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stopgap-private", "scratch", "run.lock")
NOTIFY, WATCHDOG, KEEPALIVE = "local.jobtools.job-notify", "local.jobtools.job-watchdog", "local.jobtools.claude-keepalive"
AGENTS = (NOTIFY, WATCHDOG, KEEPALIVE)
PAUSE_FILE = os.path.join(BOX, "paused")
OUTBOX = os.path.join(BOX, "outbox")
STALE_MIN = 360    # notify-send.sh gives up on a message after 6 h; older queued alerts describe old news
BACKLOG_MIN = 30   # job-notify runs every 5 min and on every outbox change, so 30 min queued means sending fails
SELF = os.path.abspath(__file__)

REMOTE_STEPS = """REMOTE STEPS (agent tools only; this script cannot reach them). Do all three, then run `status`:
  1. Claude desktop task job-desktop-hourly: enabled = {on}   (mcp__scheduled-tasks__update_scheduled_task)
  2. Cloud routine "Job discovery (hourly 6-22 ET)" trig_01KEPDNRjV6kPmUpytytPymj: enabled = {on}
     (RemoteTrigger action update, or mcp__claude-code-remote__update_trigger)
  3. Configured job dashboard, Settings!B7 (paused) = {sheet}
     (Sheets update_values). While this says yes, runs find jobs and apply to NONE.
Only when the owner names them: cloud routine "Job phone update" trig_01WJmDWtyja31qrTx57UYujv and the
Codex automations job-applications-local-shadow and job-migration-shadow-worker."""


def domain():
    return f"gui/{os.getuid()}"


def launchctl(*args):
    p = subprocess.run([LAUNCHCTL, *args], capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def agent_state(label):
    loaded = launchctl("print", f"{domain()}/{label}")[0] == 0
    out = launchctl("print-disabled", domain())[1]
    disabled = re.search(rf'"{re.escape(label)}" => (disabled|true)', out) is not None
    return {"loaded": loaded, "disabled": disabled}


def short(label):
    return label.rsplit(".", 1)[-1]


def age_min(path):
    return (time.time() - os.path.getmtime(path)) / 60


def queued():
    try:
        names = sorted(n for n in os.listdir(OUTBOX) if n.endswith(".txt"))
    except FileNotFoundError:
        return []
    return [os.path.join(OUTBOX, n) for n in names]


def lock_info():
    try:
        d = json.load(open(LOCK))
    except Exception:
        return None
    return {"mode": d.get("mode", "?"), "age_min": round(age_min(LOCK))}


def status(sheet_paused=None):
    """Return the Mac-side state and every inconsistency in it, each with a dedup key for notify.py."""
    paused_file = os.path.exists(PAUSE_FILE)
    agents = {a: agent_state(a) for a in AGENTS}
    box = queued()
    oldest = round(max((age_min(f) for f in box), default=0))
    in_run = sheet_paused is not None
    problems = []

    def problem(key, msg, hours=12):
        problems.append({"key": key, "hours": hours, "msg": msg})

    down = [short(a) for a, s in agents.items() if not s["loaded"]]
    if in_run and paused_file:
        problem("half-paused-mac", "[Jobs] Runs are on but the Mac pause file is still set, so stuck runs are not "
                f"reported. To finish resuming: python3 -I {SELF} resume. To stop runs too: say 'pause the job automation'.")
    elif not paused_file and down:
        problem("alerts-down", f"[Jobs] Phone alerts are off: {', '.join(down)} not running, so stuck runs and "
                f"blocked sites are not texted. Fix: python3 -I {SELF} resume")
    if in_run and sheet_paused == "yes":
        # The sheet flag alone is also the documented find-only mode, so remind once a day, not every run.
        problem("half-paused-sheet", "[Jobs] Runs are on but the Job Dashboard says paused = yes, so jobs are found "
                "and none are applied to. Set Settings > paused to no, or say 'pause the job automation' to stop "
                "the runs too.", hours=24)
    if box and oldest > BACKLOG_MIN and agents[NOTIFY]["loaded"]:
        problem("outbox-stuck", f"[Jobs] {len(box)} phone alerts are stuck on the Mac (oldest {oldest} min). Messages "
                "may need permission again: open Messages on the Mac and check notify.log.")
    return {
        "pause_file": paused_file,
        "agents": {short(a): s for a, s in agents.items()},
        "outbox": {"queued": len(box), "oldest_min": oldest},
        "run_lock": lock_info(),
        "sheet_paused": sheet_paused,
        "problems": problems,
    }


def print_status(s):
    print("Job automation, Mac side")
    print(f"  pause file        {'SET (paused)' if s['pause_file'] else 'absent'}")
    for name, a in s["agents"].items():
        state = "running" if a["loaded"] else "NOT RUNNING"
        print(f"  {name:<17} {state}{'  (disabled)' if a['disabled'] else ''}")
    print(f"  outbox            {s['outbox']['queued']} queued" +
          (f", oldest {s['outbox']['oldest_min']} min" if s["outbox"]["queued"] else ""))
    lk = s["run_lock"]
    lock = "{mode} run, {age_min} min old".format(**lk) if lk else "none"
    print(f"  run lock          {lock}")
    if s["sheet_paused"] is not None:
        print(f"  sheet paused      {s['sheet_paused']}")
    print("PROBLEMS:" if s["problems"] else "PROBLEMS: none")
    for p in s["problems"]:
        print(f"  - {p['key']}: {p['msg']}")


def archive_stale_outbox():
    """Move alerts too old to matter out of the outbox, so re-enabling job-notify does not text old news."""
    stale = [f for f in queued() if age_min(f) > STALE_MIN]
    if not stale:
        return []
    dest = os.path.join(BOX, "outbox-stale", datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(dest, exist_ok=True)
    for f in stale:
        shutil.move(f, dest)
        if os.path.exists(f + ".shown"):
            os.remove(f + ".shown")
    return [os.path.join(dest, os.path.basename(f)) for f in stale]


def resume():
    moved = archive_stale_outbox()
    if os.path.exists(PAUSE_FILE):
        os.remove(PAUSE_FILE)
    failed = []
    for a in AGENTS:
        launchctl("enable", f"{domain()}/{a}")
        if not agent_state(a)["loaded"]:
            rc, out = launchctl("bootstrap", domain(), os.path.join(AGENTS_DIR, a + ".plist"))
            if rc != 0 and not agent_state(a)["loaded"]:
                failed.append(f"{short(a)}: {out.strip()}")
    print(f"Mac side resumed. Archived {len(moved)} stale alert(s)" +
          (f" to {os.path.dirname(moved[0])}" if moved else "") + ".")
    for f in failed:
        print(f"  FAILED to start {f}")
    print(REMOTE_STEPS.format(on="true", sheet="no"))
    return 1 if failed else 0


def pause():
    with open(PAUSE_FILE, "w") as fh:
        fh.write(f"paused by owner {datetime.date.today().isoformat()}: job automation paused until further notice\n")
    for a in AGENTS:
        launchctl("bootout", f"{domain()}/{a}")
        launchctl("disable", f"{domain()}/{a}")
    print("Mac side paused: pause file set, alert and keepalive agents stopped and disabled.")
    print(REMOTE_STEPS.format(on="false", sheet="yes"))
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status")
    st.add_argument("--sheet-paused", choices=("yes", "no"))
    st.add_argument("--json", action="store_true")
    sub.add_parser("pause")
    sub.add_parser("resume")
    a = ap.parse_args(argv)
    if a.cmd == "pause":
        return pause()
    if a.cmd == "resume":
        rc = resume()
        print()
        print_status(status())
        return rc
    s = status(a.sheet_paused)
    print(json.dumps(s)) if a.json else print_status(s)
    return 1 if s["problems"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
