"""Each Redis user can do what its client does and nothing else, against a real Redis loaded with the rendered ACL."""
from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
import redis

_REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("redis_acl_inventory", _REPO / "scripts/redis_acl_inventory.py")
inv_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inv_mod)

_USERS = ["gateway", "config", "did", "knowledge", "conversation", "telephony", "campaigns", "freeswitch"]
_PASSWORDS = {u: f"pw-{u}-test" for u in _USERS}

pytestmark = pytest.mark.skipif(shutil.which("redis-server") is None, reason="BLOCKED-ENV: redis-server not installed")


@pytest.fixture(scope="module")
def port(tmp_path_factory):
    d = tmp_path_factory.mktemp("redis_acl")
    acl = d / "users.acl"
    env = {**os.environ, **{f"REDIS_{u.upper()}_PASSWORD": p for u, p in _PASSWORDS.items()}}
    subprocess.run([str(_REPO / "scripts/render_redis_acl.sh"), str(acl)], env=env, check=True)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
    proc = subprocess.Popen(
        ["redis-server", "--port", str(p), "--bind", "127.0.0.1", "--protected-mode", "yes", "--aclfile", str(acl),
         "--save", "", "--appendonly", "no", "--dir", str(d)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", p), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("throwaway redis did not start")
        yield p
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _as(port: int, user: str) -> redis.Redis:
    return redis.Redis(port=port, username=user, password=_PASSWORDS[user], decode_responses=True)


def _denied(call) -> bool:
    try:
        call()
    except redis.exceptions.NoPermissionError:
        return True
    return False


def test_allowed_and_denied_matrix(port):
    fs, camp, conv, tel = (_as(port, u) for u in ("freeswitch", "campaigns", "conversation", "telephony"))
    camp.set("campaign:intent:x", "v")
    camp.set("campaign:claim:x", "v")
    assert fs.execute_command("GETDEL", "campaign:intent:x") == "v"
    assert fs.set("campaign:claim:y", "v", nx=True, ex=60)
    assert fs.xadd("campaign:events:fs", {"k": "v"})

    denied = [
        lambda: fs.set("campaign:intent:x", "v"),
        lambda: fs.execute_command("GETDEL", "campaign:claim:x"),
        lambda: fs.get("did:x"),
        lambda: fs.xadd("campaign:events:cpaas", {"k": "v"}),
        lambda: fs.set("campaign:cbprop:x", "v"),
        lambda: conv.execute_command("GETDEL", "campaign:intent:x"),
        lambda: conv.xadd("campaign:events:fs", {"k": "v"}),
        lambda: tel.xadd("campaign:events:fs", {"k": "v"}),
    ]
    assert len(denied) > 0
    assert all(_denied(c) for c in denied)
    assert conv.xadd("campaign:events:conv", {"k": "v"})
    assert tel.xadd("campaign:events:cpaas", {"k": "v"})
    assert tel.set("telephony:webhook_nonce:n", "1", nx=True, ex=60)
    assert _denied(lambda: tel.get("telephony:webhook_nonce:n"))


def test_unauthenticated_ping_is_noauth(port):
    with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
        s.sendall(b"PING\r\n")
        assert s.recv(100).startswith(b"-NOAUTH")


def test_default_user_is_off(port):
    with pytest.raises(redis.AuthenticationError):
        redis.Redis(port=port, password="anything").ping()


def _narrowed(client: str, cmd: str, key: str) -> bool:
    """Telephony reads did:/agent: only, writes (never reads) its health keys, and never deletes health keys."""
    if client != "telephony":
        return False
    return (key.startswith(("did:", "agent:")) and cmd != "get") or \
           (key.startswith("telephony:health:") and cmd in {"get", "del"})


def test_every_inventory_command_succeeds_for_its_own_client(port):
    exercised = 0
    for client, entry in inv_mod.inventory().items():
        r = _as(port, client)
        keys = [k.replace("*", "x") for k in entry["keys"]]
        for cmd in entry["commands"]:
            if cmd == "scan":
                r.scan()
            elif cmd == "xadd":
                continue   # stream keys are not in the inventory's key list; the selector grants are tested above
            elif cmd in {"publish", "subscribe", "unsubscribe"}:
                for ch in entry["channels"]:
                    if cmd == "publish":
                        r.publish(ch, "m")
                    elif cmd == "subscribe":
                        ps = r.pubsub()
                        ps.subscribe(ch)
                        ps.unsubscribe(ch)
                        ps.close()
                    else:
                        continue
                    exercised += 1
                continue
            else:
                for k in (k for k in keys if not _narrowed(client, cmd, k)):
                    {"get": lambda: r.get(k), "mget": lambda: r.mget(k), "set": lambda: r.set(k, "v"),
                     "del": lambda: r.delete(k), "exists": lambda: r.exists(k)}[cmd]()
                    exercised += 1
                continue
            exercised += 1
    assert exercised > 0


def test_inventory_commands_are_denied_on_keys_outside_the_client(port):
    for client in ("gateway", "knowledge", "conversation", "telephony"):
        r = _as(port, client)
        assert _denied(lambda: r.set("campaign:cbprop:x", "v"))
        assert _denied(lambda: r.get("campaign:cbprop:x"))
    tel = _as(port, "telephony")
    for key in ("did:x", "agent:x"):
        tel.get(key)
        assert _denied(lambda: tel.set(key, "v"))
        assert _denied(lambda: tel.delete(key))
    assert _denied(lambda: tel.publish("provider_config_changed", "m"))
    assert _denied(lambda: _as(port, "did").get("did:x"))
    assert _denied(lambda: _as(port, "gateway").set("did:x", "v"))
