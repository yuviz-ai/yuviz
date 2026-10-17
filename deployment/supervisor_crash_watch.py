"""Restart policy for supervisord: heartbeat, exponential backoff, half-open circuit breaker. stdout is the protocol; log to stderr."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import xmlrpc.client
from dataclasses import asdict, dataclass, field
from pathlib import Path

RUNNING_STATES = {"STARTING", "RUNNING", "BACKOFF"}
FATAL = "FATAL"
PROBE_INTERVAL_S = 10
MISSES_TO_RESTART = 3
STARTUP_DEADLINE_S = 300


@dataclass
class ServiceState:
    crashes: list = field(default_factory=list)
    opens: int = 0
    half_open_since: float | None = None
    pending_at: float | None = None
    pending_trial: bool = False
    stop_token: object = None  # supervisord's 'stop' time of our stop, or FATAL


class Policy:
    def __init__(self, max_crashes: int, window_s: float, backoff_cap_s: float, cooldown_s: float, cooldown_cap_s: float):
        self.max_crashes = max_crashes
        self.window_s = window_s
        self.backoff_cap_s = backoff_cap_s
        self.cooldown_s = cooldown_s
        self.cooldown_cap_s = cooldown_cap_s
        self.services: dict[str, ServiceState] = {}

    def state(self, name: str) -> ServiceState:
        return self.services.setdefault(name, ServiceState())

    def on_exit(self, name: str, now: float) -> tuple[str, float]:
        s = self.state(name)
        if s.half_open_since is not None and now - s.half_open_since < self.window_s:
            return self._open(s)
        s.half_open_since = None
        s.crashes = [t for t in s.crashes if now - t <= self.window_s] + [now]
        n = len(s.crashes)
        if n >= self.max_crashes:
            return self._open(s)
        if n == 1:
            return ("restart", 0.0)
        return ("delay", min(2.0 ** (n - 1), self.backoff_cap_s))

    def on_fatal(self, name: str) -> tuple[str, float]:
        return self._open(self.state(name))

    def _open(self, s: ServiceState) -> tuple[str, float]:
        cooldown = min(self.cooldown_s * 2 ** s.opens, self.cooldown_cap_s)
        s.opens += 1
        s.crashes = []
        s.half_open_since = None
        return ("open", cooldown)

    def close_if_survived(self, name: str, now: float) -> bool:
        s = self.state(name)
        if s.half_open_since is not None and now - s.half_open_since >= self.window_s:
            s.opens, s.half_open_since = 0, None
            return True
        return False


def parse_tokens(line: str) -> dict[str, str]:
    return dict(token.split(":", 1) for token in line.split())


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost")
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._path)


class _UnixTransport(xmlrpc.client.Transport):
    def __init__(self, path: str):
        super().__init__()
        self._path = path

    def make_connection(self, host):
        return _UnixHTTPConnection(self._path)


def rpc_proxy(server_url: str):
    if server_url.startswith("unix://"):
        return xmlrpc.client.ServerProxy("http://localhost", transport=_UnixTransport(server_url[len("unix://"):]))
    return xmlrpc.client.ServerProxy(server_url.rstrip("/") + "/RPC2")


def log(msg: str) -> None:
    sys.stderr.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    sys.stderr.flush()


LOG_FILE = Path.home() / ".yuviz" / "logs" / "crash-watch.log"


def notify(msg: str) -> None:
    if sys.platform != "darwin":
        return
    notifier = shutil.which("terminal-notifier") or next(
        (p for p in ("/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier") if os.path.exists(p)), None)
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "timeout": 5, "check": False}
    if notifier:  # clicking opens the log
        done = subprocess.run([notifier, "-title", "yuviz crash-watch", "-message", msg, "-open", LOG_FILE.as_uri(),
                               "-group", "yuviz-crash-watch"], **quiet)
        if done is not None and done.returncode == 0:
            return
    # Credited to Script Editor, but always allowed.
    subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "yuviz crash-watch"'], **quiet)


def stop_service(proxy, name: str, settle_s: float = 60.0, kill_now: bool = False) -> float | None:
    """Idempotent. Returns supervisord's stop time once STOPPED, or None if it never got there.
    kill_now: a hung process can't shut down gracefully, so SIGKILL its group instead of waiting stopwaitsecs."""
    deadline = time.monotonic() + settle_s
    while time.monotonic() < deadline:
        info = proxy.supervisor.getProcessInfo(name)
        if info["statename"] == "STOPPED":
            return float(info["stop"])
        if info["statename"] in RUNNING_STATES:
            try:
                proxy.supervisor.stopProcess(name, False)  # first, so supervisord treats the exit as a stop, not a crash
            except xmlrpc.client.Fault:
                pass  # raced a state change; re-read
        if kill_now and info["statename"] in RUNNING_STATES | {"STOPPING"}:
            kill_group(int(info.get("pid") or 0))
        time.sleep(0.2)
    return None


def kill_group(pid: int) -> None:
    """supervisord makes each program its own process group led by the wrapper."""
    if pid <= 1:
        return
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def start_service(proxy, name: str) -> None:
    """Idempotent: already running is success."""
    try:
        proxy.supervisor.startProcess(name, False)
    except xmlrpc.client.Fault as exc:
        if "ALREADY_STARTED" not in exc.faultString:
            raise


def probe_http(url: str, timeout: float = 3.0) -> bool:
    """Any HTTP response proves the event loop answered."""
    try:
        urllib.request.urlopen(url, timeout=timeout).close()
        return True
    except urllib.error.HTTPError:
        return True
    except (OSError, ValueError):
        return False


def probe_envoy_host(addr: str, admin: str = "http://127.0.0.1:9901", timeout: float = 3.0) -> bool | None:
    """Envoy's own gRPC health check for one upstream; None when Envoy can't say."""
    try:
        with urllib.request.urlopen(f"{admin}/clusters", timeout=timeout) as r:
            text = r.read().decode(errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if f"::{addr}::health_flags::" in line:
            flags = line.rsplit("::", 1)[1]
            if flags == "healthy":
                return True
            return False if "failed_active_hc" in flags else None
    return None


def probe_esl(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            data = b""
            while b"auth/request" not in data:
                chunk = sock.recv(256)
                if not chunk:
                    return False
                data += chunk
        return True
    except OSError:
        return False


def probe_sip(host: str, port: int, timeout: float = 3.0) -> bool:
    """SIP OPTIONS; any SIP reply means Kamailio is processing requests."""
    tag, call_id = uuid.uuid4().hex[:8], uuid.uuid4().hex
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.bind((host, 0))
            lport = sock.getsockname()[1]
            msg = (f"OPTIONS sip:{host}:{port} SIP/2.0\r\nVia: SIP/2.0/UDP {host}:{lport};branch=z9hG4bK{tag}\r\n"
                   f"Max-Forwards: 70\r\nFrom: <sip:crash-watch@{host}>;tag={tag}\r\nTo: <sip:{host}:{port}>\r\n"
                   f"Call-ID: {call_id}\r\nCSeq: 1 OPTIONS\r\nContent-Length: 0\r\n\r\n")
            sock.sendto(msg.encode(), (host, port))
            return sock.recv(4096).startswith(b"SIP/2.0 ")
    except OSError:
        return False


def kamailio_listen(cfg: Path) -> tuple[str, int] | None:
    try:
        for line in cfg.read_text().splitlines():
            line = line.strip()
            if line.startswith("listen=udp:"):
                host, _, port = line[len("listen=udp:"):].rpartition(":")
                return host, int(port)
    except (OSError, ValueError):
        pass
    return None


def dotenv(key: str, default: str, path: Path = Path(".env")) -> str:
    if os.environ.get(key):
        return os.environ[key]
    try:
        for line in path.read_text().splitlines():
            if line.startswith(f"{key}=") and line.split("=", 1)[1].strip():
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return default


def default_probes() -> dict:
    host = dotenv("LISTEN_HOST", "127.0.0.1")
    host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    esl = (dotenv("FREESWITCH_ESL_HOST", "127.0.0.1"), int(dotenv("FREESWITCH_ESL_PORT", "8022")))
    gw_host = dotenv("GATEWAY_LISTEN_HOST", "127.0.0.1")
    gw_host = "127.0.0.1" if gw_host in ("0.0.0.0", "::", "") else gw_host
    sip = kamailio_listen(Path.home() / ".yuviz" / "kamailio" / "kamailio.cfg")
    probes = {
        "config": lambda: probe_http(f"http://{host}:8000/health"),
        "knowledge": lambda: probe_http(f"http://{host}:8100/health"),
        "campaigns": lambda: probe_http(f"http://{host}:8400/health"),
        "toolexec": lambda: probe_http(f"http://{host}:8600/health"),
        "telephony": lambda: probe_http(f"http://{host}:8750/health"),
        "did": lambda: probe_http(f"http://{host}:8200/health"),
        "webcall": lambda: probe_http(f"http://{host}:8300/health"),
        "ollama": lambda: probe_http("http://127.0.0.1:11434/api/version"),
        "envoy": lambda: probe_http("http://127.0.0.1:9901/ready"),
        "admin-ui": lambda: probe_http(f"http://{host}:3000/login", timeout=15),  # dev server compiles on demand
        "conversation-1": lambda: probe_envoy_host("127.0.0.1:50051"),
        "conversation-2": lambda: probe_envoy_host("127.0.0.1:50052"),
        "freeswitch": lambda: probe_esl(*esl),
        "gateway": lambda: probe_http(f"http://{gw_host}:8080/health"),
    }
    if sip:
        probes["kamailio"] = lambda: probe_sip(*sip)
    return probes


class Watcher:
    def __init__(self, policy: Policy, proxy_factory, state_path: Path, schedule=None, clock=time.time):
        self.policy = policy
        self.proxy = proxy_factory  # one client per call: xmlrpc clients are not thread-safe
        self.state_path = state_path
        self.clock = clock
        self.schedule = schedule or self._timer
        self.lock = threading.RLock()
        self.misses: dict[str, int] = {}
        self.armed: dict[str, float] = {}  # name -> start time it first passed a probe
        self.alerted: dict[str, float] = {}

    def probe_once(self, probes: dict) -> None:
        proxy = self.proxy()
        for name, probe in probes.items():
            try:
                info = proxy.supervisor.getProcessInfo(name)
            except xmlrpc.client.Fault:
                continue
            start = float(info["start"])
            if info["statename"] != "RUNNING":
                self.misses.pop(name, None)
                continue
            ok = probe()
            if ok is None:
                continue  # can't tell, e.g. Envoy itself is down
            if ok:
                self.misses[name] = 0
                self.armed[name] = start
                continue
            if self.armed.get(name) != start:
                # Never answered since start: a slow starter or a wrong probe, so alert once, never restart.
                if self.clock() - start >= STARTUP_DEADLINE_S and self.alerted.get(name) != start:
                    self.alerted[name] = start
                    msg = f"{name} has not passed its heartbeat in {STARTUP_DEADLINE_S}s since start; check it"
                    log(msg)
                    notify(msg)
                continue
            n = self.misses[name] = self.misses.get(name, 0) + 1
            log(f"{name} missed heartbeat ({n}/{MISSES_TO_RESTART})")
            if n >= MISSES_TO_RESTART:
                self.misses[name] = 0
                self.armed.pop(name, None)
                self.on_hang(name)

    def probe_loop(self, probes: dict, interval_s: float) -> None:
        while True:
            try:
                self.probe_once(probes)
            except Exception as exc:  # noqa: BLE001
                log(f"heartbeat round failed: {exc!r}")
            time.sleep(interval_s)

    @staticmethod
    def _timer(delay: float, fn) -> None:
        t = threading.Timer(max(0.0, delay), fn)
        t.daemon = True
        t.start()

    def load(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        self.policy.services = {name: ServiceState(**s) for name, s in raw.items()}

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({n: asdict(s) for n, s in self.policy.services.items()}))
        tmp.replace(self.state_path)

    def resume(self) -> None:
        now = self.clock()
        with self.lock:
            for name, s in self.policy.services.items():
                if s.pending_at is not None:
                    log(f"{name}: resuming pending {'trial' if s.pending_trial else 'restart'}")
                    self.schedule(s.pending_at - now, lambda n=name, due=s.pending_at: self.fire(n, due))
                if s.half_open_since is not None:
                    self.schedule(s.half_open_since + self.policy.window_s - now, lambda n=name: self.check_closed(n))

    def on_event(self, eventname: str, name: str, expected: str) -> None:
        with self.lock:
            if eventname == "PROCESS_STATE_EXITED" and expected == "0":
                action, delay = self.policy.on_exit(name, self.clock())
            elif eventname == "PROCESS_STATE_FATAL":
                action, delay = self.policy.on_fatal(name)
            else:
                return
            if action == "restart":
                log(f"{name} crashed; supervisord restarts it now")
                self.save()
                return
        self._recover(name, action, delay, fatal=eventname == "PROCESS_STATE_FATAL", what="crashed again")

    def on_hang(self, name: str) -> None:
        """A hung process never exits, so it is stopped here and counted as a crash."""
        with self.lock:
            action, delay = self.policy.on_exit(name, self.clock())
        self._recover(name, "delay" if action == "restart" else action, delay, fatal=False, what="hung (no heartbeat)",
                      kill_now=True)

    def _recover(self, name: str, action: str, delay: float, *, fatal: bool, what: str, kill_now: bool = False) -> None:
        with self.lock:
            s = self.policy.state(name)
            s.pending_at, s.pending_trial = self.clock() + delay, action == "open"
            s.stop_token = FATAL if fatal else None
            self.save()  # intent first: a restart of this listener finds it
        if not fatal:
            token = stop_service(self.proxy(), name, kill_now=kill_now)
            with self.lock:
                s.stop_token = token
                self.save()
        if action == "delay":
            log(f"{name} {what}; restarting in {delay:.0f}s")
        else:
            msg = f"{name} keeps failing; circuit open, next trial start in {delay:.0f}s"
            log(msg)
            notify(msg)
        due = s.pending_at
        self.schedule(delay, lambda: self.fire(name, due))

    def fire(self, name: str, due: float) -> None:
        with self.lock:
            s = self.policy.state(name)
            if s.pending_at != due:
                return  # already handled, or superseded
            proxy = self.proxy()
            info = proxy.supervisor.getProcessInfo(name)
            if s.stop_token == FATAL:
                ours = info["statename"] == FATAL
            else:
                ours = info["statename"] == "STOPPED" and (s.stop_token is None or abs(float(info["stop"]) - s.stop_token) < 1)
            trial = s.pending_trial
            s.pending_at, s.pending_trial, s.stop_token = None, False, None
            if ours:
                start_service(proxy, name)
                if trial:
                    s.half_open_since = self.clock()
                    self.schedule(self.policy.window_s + 1, lambda: self.check_closed(name))
            else:
                s.opens, s.crashes, s.half_open_since = 0, [], None  # a human took over
            self.save()
        log(f"{name}: {'trial start (half-open)' if trial else 'restarted after backoff'}" if ours
            else f"{name}: changed by hand since ({info['statename']}); leaving it alone")

    def check_closed(self, name: str) -> None:
        with self.lock:
            s = self.policy.state(name)
            if s.half_open_since is None:
                return
            remaining = s.half_open_since + self.policy.window_s - self.clock()
            if remaining > 0:  # timers can fire a hair early
                self.schedule(remaining + 1, lambda: self.check_closed(name))
                return
            self.policy.close_if_survived(name, self.clock())
            self.save()
        log(f"{name}: survived {self.policy.window_s:.0f}s after trial; circuit closed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-crashes", type=int, default=5)
    parser.add_argument("--window", type=float, default=600)
    parser.add_argument("--backoff-cap", type=float, default=300)
    parser.add_argument("--cooldown", type=float, default=300)
    parser.add_argument("--cooldown-cap", type=float, default=3600)
    parser.add_argument("--state", type=Path, default=Path.home() / ".yuviz" / "crash-watch.json")
    parser.add_argument("--no-heartbeat", action="store_true")
    args = parser.parse_args()

    policy = Policy(args.max_crashes, args.window, args.backoff_cap, args.cooldown, args.cooldown_cap)
    url = os.environ["SUPERVISOR_SERVER_URL"]
    watcher = Watcher(policy, lambda: rpc_proxy(url), args.state)
    watcher.load()
    watcher.resume()
    if not args.no_heartbeat:
        threading.Thread(target=watcher.probe_loop, args=(default_probes(), PROBE_INTERVAL_S), daemon=True).start()
    me = os.environ.get("SUPERVISOR_PROCESS_NAME")
    log(f"watching: backoff to {args.backoff_cap:.0f}s, circuit opens at {args.max_crashes} crashes in {args.window:.0f}s, "
        f"heartbeat {'off' if args.no_heartbeat else f'every {PROBE_INTERVAL_S}s, restart after {MISSES_TO_RESTART} misses'}")
    while True:
        sys.stdout.write("READY\n")
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            return
        header = parse_tokens(line)
        payload = sys.stdin.read(int(header.get("len", 0)))
        first_line = payload.split("\n", 1)[0]
        event = parse_tokens(first_line) if first_line else {}
        name = event.get("processname", "")
        if name and name != me:
            try:
                watcher.on_event(header.get("eventname", ""), name, event.get("expected", ""))
            except Exception as exc:  # noqa: BLE001
                log(f"{name}: handling {header.get('eventname')} failed: {exc!r}")
        sys.stdout.write("RESULT 2\nOK")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
