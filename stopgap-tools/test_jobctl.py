"""Tests for jobctl.py, run against a fake launchctl and a temp watchdog folder (nothing real is touched).

The key case is the 2026-11-19 incident: runs re-enabled, sheet still paused = yes, alert agents still off.
"""
import json, os, subprocess, sys, textwrap, time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
JOBCTL = os.path.join(HERE, "jobctl.py")
AGENTS = ["local.jobtools.job-notify", "local.jobtools.job-watchdog", "local.jobtools.claude-keepalive"]

FAKE_LAUNCHCTL = textwrap.dedent('''\
    #!/usr/bin/env python3
    # Minimal launchctl stand-in. State lives in $FAKE_LC_STATE: {"loaded": [...], "disabled": [...]}
    import json, os, sys
    p = os.environ["FAKE_LC_STATE"]
    s = json.load(open(p))
    cmd, args = sys.argv[1], sys.argv[2:]
    label = lambda t: t.split("/", 2)[-1]
    if cmd == "print":
        sys.exit(0 if label(args[0]) in s["loaded"] else 113)
    if cmd == "print-disabled":
        print("disabled services = {")
        for l in s["disabled"]:
            print('\\t"%s" => disabled' % l)
        print("}")
        sys.exit(0)
    if cmd == "enable":
        s["disabled"] = [l for l in s["disabled"] if l != label(args[0])]
    elif cmd == "disable":
        s["disabled"] = sorted(set(s["disabled"]) | {label(args[0])})
    elif cmd == "bootout":
        s["loaded"] = [l for l in s["loaded"] if l != label(args[0])]
    elif cmd == "bootstrap":
        l = os.path.basename(args[1])[:-len(".plist")]
        if l in s["disabled"] or not os.path.exists(args[1]):
            print("Bootstrap failed: 5: Input/output error"); sys.exit(5)
        s["loaded"] = sorted(set(s["loaded"]) | {l})
    json.dump(s, open(p, "w"))
''')


@pytest.fixture
def env(tmp_path):
    box, agents_dir = tmp_path / "job-watchdog", tmp_path / "LaunchAgents"
    (box / "outbox").mkdir(parents=True)
    agents_dir.mkdir()
    for a in AGENTS:
        (agents_dir / f"{a}.plist").write_text("<plist/>")
    lc = tmp_path / "launchctl"
    lc.write_text(FAKE_LAUNCHCTL)
    lc.chmod(0o755)
    state = tmp_path / "lc-state.json"
    e = dict(os.environ, JOBCTL_BOX=str(box), JOBCTL_AGENTS_DIR=str(agents_dir), JOBCTL_LAUNCHCTL=str(lc),
             JOBCTL_LOCK=str(tmp_path / "run.lock"), FAKE_LC_STATE=str(state))

    class Env:
        path = tmp_path

        def set_agents(self, loaded, disabled):
            state.write_text(json.dumps({"loaded": loaded, "disabled": disabled}))

        def agents(self):
            return json.loads(state.read_text())

        def run(self, *args):
            p = subprocess.run([sys.executable, "-I", JOBCTL, *args], env=e, capture_output=True, text=True)
            return p.returncode, p.stdout, p.stderr

        def status(self, *args):
            rc, out, err = self.run("status", "--json", *args)
            assert err == "", err
            return rc, json.loads(out)

        def queue(self, name, minutes_old):
            f = box / "outbox" / name
            f.write_text("[Jobs] test")
            t = time.time() - minutes_old * 60
            os.utime(f, (t, t))
            return f

    Env.box = box
    return Env()


def keys(s):
    return sorted(p["key"] for p in s["problems"])


def test_all_on_is_clean(env):
    env.set_agents(AGENTS, [])
    rc, s = env.status("--sheet-paused", "no")
    assert rc == 0 and s["problems"] == []


def test_incident_2026_11_19_half_resume_is_reported(env):
    # Task re-enabled (we are inside a run), pause file removed, but sheet still yes and agents still off.
    env.set_agents([], AGENTS)
    rc, s = env.status("--sheet-paused", "yes")
    assert rc == 1
    assert keys(s) == ["alerts-down", "half-paused-sheet"]
    assert next(p for p in s["problems"] if p["key"] == "half-paused-sheet")["hours"] == 24


def test_run_with_mac_pause_file_still_set_is_reported(env):
    env.set_agents([], AGENTS)
    (env.box / "paused").write_text("paused")
    rc, s = env.status("--sheet-paused", "no")
    assert rc == 1 and keys(s) == ["half-paused-mac"]


def test_full_pause_outside_a_run_is_consistent(env):
    env.set_agents(AGENTS, [])
    rc, out, _ = env.run("pause")
    assert rc == 0 and "Settings!B7 (paused) = yes" in out
    assert (env.box / "paused").exists()
    assert env.agents() == {"loaded": [], "disabled": sorted(AGENTS)}
    rc, s = env.status()
    assert rc == 0 and s["problems"] == []


def test_resume_restores_mac_side_and_archives_stale_alerts(env):
    env.set_agents([], AGENTS)
    (env.box / "paused").write_text("paused")
    stale = env.queue("20261120-061040-aaaaaa.txt", minutes_old=24 * 60)
    fresh = env.queue("20261121-061040-bbbbbb.txt", minutes_old=5)
    rc, out, _ = env.run("resume")
    assert rc == 0, out
    assert not (env.box / "paused").exists()
    assert sorted(env.agents()["loaded"]) == sorted(AGENTS) and env.agents()["disabled"] == []
    assert not stale.exists() and fresh.exists()
    archived = list((env.box / "outbox-stale").rglob("*.txt"))
    assert [p.name for p in archived] == [stale.name]
    assert "Settings!B7 (paused) = no" in out and "enabled = true" in out
    rc, s = env.status("--sheet-paused", "no")
    assert rc == 0 and s["problems"] == []


def test_resume_reports_an_agent_that_will_not_start(env):
    env.set_agents([], [])
    (env.path / "LaunchAgents" / "local.jobtools.job-notify.plist").unlink()
    rc, out, _ = env.run("resume")
    assert rc == 1 and "FAILED to start job-notify" in out


def test_stuck_outbox_is_reported_only_when_the_sender_runs(env):
    env.set_agents(AGENTS, [])
    env.queue("20261121-050000-cccccc.txt", minutes_old=45)
    rc, s = env.status("--sheet-paused", "no")
    assert keys(s) == ["outbox-stuck"]
    env.set_agents([a for a in AGENTS if a != "local.jobtools.job-notify"], [])
    rc, s = env.status("--sheet-paused", "no")
    assert keys(s) == ["alerts-down"]
