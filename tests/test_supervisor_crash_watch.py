import importlib.util
import sys
import xmlrpc.client
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "deployment" / "supervisor_crash_watch.py"
_spec = importlib.util.spec_from_file_location("supervisor_crash_watch", _PATH)
cw = importlib.util.module_from_spec(_spec)
sys.modules["supervisor_crash_watch"] = cw
_spec.loader.exec_module(cw)


def _policy():
    return cw.Policy(max_crashes=5, window_s=600, backoff_cap_s=300, cooldown_s=300, cooldown_cap_s=3600)


def test_backoff_doubles_then_the_circuit_opens():
    p = _policy()
    assert [p.on_exit("gw", t) for t in (0, 10, 20, 30)] == [("restart", 0), ("delay", 2), ("delay", 4), ("delay", 8)]
    assert p.on_exit("gw", 40) == ("open", 300)


def test_backoff_resets_once_old_crashes_leave_the_window():
    p = _policy()
    p.on_exit("gw", 0)
    p.on_exit("gw", 10)
    assert p.on_exit("gw", 700) == ("restart", 0)


def test_cooldown_doubles_per_reopen_and_is_capped():
    p = _policy()
    assert [p.on_fatal("gw")[1] for _ in range(6)] == [300, 600, 1200, 2400, 3600, 3600]


def test_a_crash_during_the_trial_reopens_and_survival_closes():
    p = _policy()
    p.on_fatal("gw")
    p.state("gw").half_open_since = 1000
    assert p.on_exit("gw", 1100) == ("open", 600)
    p.state("gw").half_open_since = 2000
    assert not p.close_if_survived("gw", 2500)
    assert p.close_if_survived("gw", 2600)
    assert p.state("gw").opens == 0


def test_parses_supervisor_header_and_payload_tokens():
    header = cw.parse_tokens("ver:3.0 server:supervisor serial:21 pool:crash-watch poolserial:10 "
                             "eventname:PROCESS_STATE_EXITED len:84\n")
    assert header["eventname"] == "PROCESS_STATE_EXITED" and header["len"] == "84"
    event = cw.parse_tokens("processname:gateway groupname:gateway from_state:RUNNING expected:0 pid:2766")
    assert event["processname"] == "gateway" and event["expected"] == "0"


class _FakeSupervisor:
    def __init__(self, state="RUNNING"):
        self.statename = state
        self.stop = 0.0
        self.starts = 0
        self.stops = 0

    def getProcessInfo(self, name):
        return {"statename": self.statename, "stop": self.stop}

    def stopProcess(self, name, wait):
        self.stops += 1
        self.statename, self.stop = "STOPPED", 500.0
        return True

    def startProcess(self, name, wait):
        if self.statename in cw.RUNNING_STATES:
            raise xmlrpc.client.Fault(60, "ALREADY_STARTED: " + name)
        self.starts += 1
        self.statename = "RUNNING"
        return True


class _FakeProxy:
    def __init__(self, sup):
        self.supervisor = sup


def _watcher(tmp_path, sup, clock):
    timers = []
    w = cw.Watcher(_policy(), lambda: _FakeProxy(sup), tmp_path / "state.json",
                   schedule=lambda delay, fn: timers.append((delay, fn)), clock=lambda: clock[0])
    return w, timers


def test_stop_and_start_are_idempotent():
    sup = _FakeSupervisor("STOPPED")
    assert cw.stop_service(_FakeProxy(sup), "gw", settle_s=1) == 0.0 and sup.stops == 0
    sup.statename = "RUNNING"
    cw.start_service(_FakeProxy(sup), "gw")
    assert sup.starts == 0


def test_delayed_restart_fires_once_even_if_run_twice(tmp_path):
    sup, clock = _FakeSupervisor(), [100.0]
    w, timers = _watcher(tmp_path, sup, clock)
    w.on_event("PROCESS_STATE_EXITED", "gw", "0")
    sup.statename = "STARTING"  # supervisord respawned it
    w.on_event("PROCESS_STATE_EXITED", "gw", "0")
    assert sup.statename == "STOPPED" and timers[-1][0] == 2
    timers[-1][1]()
    timers[-1][1]()
    assert sup.starts == 1


def test_a_service_stopped_by_hand_is_left_alone(tmp_path):
    sup, clock = _FakeSupervisor(), [100.0]
    w, timers = _watcher(tmp_path, sup, clock)
    w.on_event("PROCESS_STATE_EXITED", "gw", "0")
    w.on_event("PROCESS_STATE_EXITED", "gw", "0")
    sup.stop = 900.0  # someone ran `supervisorctl stop` later
    timers[-1][1]()
    assert sup.starts == 0


def test_a_restarted_watcher_resumes_the_pending_restart(tmp_path):
    sup, clock = _FakeSupervisor(), [100.0]
    w, _ = _watcher(tmp_path, sup, clock)
    w.on_event("PROCESS_STATE_EXITED", "gw", "0")
    w.on_event("PROCESS_STATE_EXITED", "gw", "0")

    clock[0] = 101.0
    w2, timers2 = _watcher(tmp_path, sup, clock)
    w2.load()
    w2.resume()
    assert len(timers2) == 1 and timers2[0][0] == 1.0
    timers2[0][1]()
    assert sup.starts == 1


def test_fatal_opens_the_circuit_and_the_trial_starts_it(tmp_path):
    sup, clock = _FakeSupervisor("FATAL"), [100.0]
    w, timers = _watcher(tmp_path, sup, clock)
    w.on_event("PROCESS_STATE_FATAL", "gw", "")
    assert timers[-1][0] == 300 and sup.stops == 0
    timers[-1][1]()
    assert sup.starts == 1 and w.policy.state("gw").half_open_since == 100.0


def test_an_early_close_check_reschedules_instead_of_giving_up(tmp_path):
    sup, clock = _FakeSupervisor("FATAL"), [100.0]
    w, timers = _watcher(tmp_path, sup, clock)
    w.on_event("PROCESS_STATE_FATAL", "gw", "")
    timers[-1][1]()  # trial start at t=100
    clock[0] = 699.99  # the timer fired a hair early
    timers[-1][1]()
    assert w.policy.state("gw").half_open_since == 100.0
    clock[0] = 701.0
    timers[-1][1]()
    assert w.policy.state("gw").half_open_since is None and w.policy.state("gw").opens == 0


def test_a_hand_start_resets_the_breaker(tmp_path):
    sup, clock = _FakeSupervisor("FATAL"), [100.0]
    w, timers = _watcher(tmp_path, sup, clock)
    w.on_event("PROCESS_STATE_FATAL", "gw", "")
    sup.statename = "RUNNING"  # someone ran `supervisorctl start`
    timers[-1][1]()
    assert sup.starts == 0 and w.policy.state("gw").opens == 0


def test_notify_prefers_terminal_notifier_and_opens_the_log(monkeypatch):
    calls = []
    monkeypatch.setattr(cw.sys, "platform", "darwin")
    rc = [0]

    def run(cmd, **kw):
        calls.append(cmd)
        return cw.subprocess.CompletedProcess(cmd, rc[0])

    monkeypatch.setattr(cw.subprocess, "run", run)
    monkeypatch.setattr(cw.shutil, "which", lambda name: "/opt/homebrew/bin/terminal-notifier")
    cw.notify("gw keeps failing")
    assert len(calls) == 1 and calls[-1][0].endswith("terminal-notifier") and cw.LOG_FILE.as_uri() in calls[-1]

    rc[0] = 3  # notifications not allowed for terminal-notifier
    cw.notify("gw keeps failing")
    assert calls[-1][0] == "osascript"

    monkeypatch.setattr(cw.shutil, "which", lambda name: None)
    monkeypatch.setattr(cw.os.path, "exists", lambda p: False)
    cw.notify("gw keeps failing")
    assert calls[-1][0] == "osascript"
