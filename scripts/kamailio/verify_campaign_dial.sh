#!/usr/bin/env bash
# Live verification of the campaign dial path (Path A) on a two-node rig: Kamailio's loopback dial handler, the PSTN
# egress token, the trigger INVITE and the FreeSWITCH Lua. Exits 0 only if every check passes.
#
# Run it against an isolated rig, never the live stack: it stops both FreeSWITCH nodes at the end. Each node runs
# the repo's dialplan, profiles and Lua; a stand-in carrier answers the egress INVITEs. Configuration (all required):
#   VERIFY_DIAL_V6 / VERIFY_DIAL_V4   dial handler base URLs, e.g. http://[::1]:15071 (allowed) and http://127.0.0.1:15071
#                                     (must be refused: the rig sets CAMPAIGNS_DIAL_SRC to ::1 only)
#   VERIFY_DIAL_SECRET, VERIFY_EGRESS_SECRET, VERIFY_FS_EGRESS_PASSWORD   as given to update_kamailio_ip.sh
#   VERIFY_KAM_SIP        host:port of Kamailio's SIP listener      VERIFY_SIP_DOMAIN  its domain (the subscriber domain)
#   VERIFY_SIP_FROM_DOMAIN  the From host the egress gateway sends (default: VERIFY_SIP_DOMAIN; add :port on a non-5060 rig)
#   VERIFY_KAM_CTL        kamcmd socket, e.g. tcp:127.0.0.1:15099   VERIFY_KAM_LOG     Kamailio's log file
#   VERIFY_SOFTPHONE      user:password of a softphone subscriber
#   VERIFY_REDIS          redis://campaigns:<pw>@host:port (writes intents)   VERIFY_REDIS_READER  a user that may XRANGE campaign:events:fs
#   VERIFY_NODES          "A|host:sip_port|esl_port|log_file B|..." (two nodes)   VERIFY_ESL_PASSWORD  their ESL password
#   VERIFY_STUB_LOG       log file of the node hosting the stand-in carrier (it logs PSTN_STUB_INVITE)
#   VERIFY_FS_CLI, VERIFY_KAMCMD   paths to fs_cli and kamcmd
#   VERIFY_LAN_IP         a non-loopback address of this host (default: en0)   VERIFY_PYTHON  python with the repo's deps
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
for name in VERIFY_DIAL_V6 VERIFY_DIAL_V4 VERIFY_DIAL_SECRET VERIFY_EGRESS_SECRET VERIFY_FS_EGRESS_PASSWORD VERIFY_KAM_SIP \
            VERIFY_SIP_DOMAIN VERIFY_KAM_CTL VERIFY_KAM_LOG VERIFY_SOFTPHONE VERIFY_REDIS VERIFY_REDIS_READER VERIFY_NODES \
            VERIFY_ESL_PASSWORD VERIFY_STUB_LOG VERIFY_FS_CLI VERIFY_KAMCMD; do
  [[ -n "${!name:-}" ]] || { echo "ERROR: $name is not set (see the header of $0)" >&2; exit 2; }
done
export VERIFY_LAN_IP="${VERIFY_LAN_IP:-$(ipconfig getifaddr en0 2>/dev/null || true)}"
export VERIFY_REPO="$REPO"
exec "${VERIFY_PYTHON:-$REPO/venv/bin/python}" -I - <<'PY'
import hashlib, http.client, json, os, re, socket, subprocess, sys, time, uuid
from dataclasses import asdict, replace
from urllib.parse import urlparse

E = os.environ
sys.path.insert(0, E["VERIFY_REPO"])
from services.campaigns import dial_request as dr  # noqa: E402

DIAL_SECRET, EGRESS_SECRET = E["VERIFY_DIAL_SECRET"].encode(), E["VERIFY_EGRESS_SECRET"].encode()
DOMAIN = E["VERIFY_SIP_DOMAIN"]
FROM_DOMAIN = E.get("VERIFY_SIP_FROM_DOMAIN", DOMAIN)
KAM_HOST, KAM_PORT = E["VERIFY_KAM_SIP"].rsplit(":", 1)
KAM = (KAM_HOST, int(KAM_PORT))
NODES = {}
for spec in E["VERIFY_NODES"].split():
    name, sip, esl, log = spec.split("|")
    host, port = sip.rsplit(":", 1)
    NODES[name] = dict(sip=(host, int(port)), esl=esl, log=log)
CALLER_ID, TENANT_SLUG, AGENT_SLUG, TENANT = "+15559990001", "rig-tenant", "rig-agent", str(uuid.uuid4())
results: list[tuple[bool, str]] = []


def check(name, ok, detail=""):
    results.append((bool(ok), name))
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   [{detail}]"), flush=True)


def read(path):
    return open(path, errors="replace").read()


def count(path, pattern):
    return len(re.findall(pattern, read(path)))


def node_logs():
    return [n["log"] for n in NODES.values()]


def redis(url, *args):
    u = urlparse(url)
    return subprocess.run(["redis-cli", "--raw", "-h", u.hostname, "-p", str(u.port), "--user", u.username, "--pass", u.password,
                           "--no-auth-warning", *args], capture_output=True, text=True, check=True).stdout


def kam(*args):
    return subprocess.run([E["VERIFY_KAMCMD"], "-s", E["VERIFY_KAM_CTL"], *args], capture_output=True, text=True, check=True).stdout


def server_transactions():
    stats = dict(re.findall(r"(\w+): (\d+)", kam("tm.stats")))
    return int(stats["total"]) - int(stats["total_local"])   # the dispatcher's own OPTIONS pings are local


def post(base, path, raw, sig=None, method="POST"):
    u = urlparse(base)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    headers = {"X-Campaign-Signature": sig if sig is not None else dr._mac(DIAL_SECRET, raw), "Content-Type": "application/json"}
    c.request(method, path, body=raw if method == "POST" else None, headers=headers)
    r = c.getresponse()
    return r.status, r.read().decode()


def new_intent(callee, *, max_call_s=10, caller_id=CALLER_ID):
    key = f"{uuid.uuid4()}_1"
    intent = dr.build_intent(key=key, tenant_id=TENANT, campaign_id=str(uuid.uuid4()), contact_id=str(uuid.uuid4()),
                             call_uuid=str(uuid.uuid4()), callee=callee, caller_id=caller_id, tenant_slug=TENANT_SLUG, agent_slug=AGENT_SLUG,
                             max_call_s=max_call_s, egress_secret=EGRESS_SECRET)
    return intent


def write_intent(intent):
    out = redis(E["VERIFY_REDIS"], "set", f"campaign:intent:{intent.key}", json.dumps(asdict(intent)), "EX", "60").strip()
    assert out == "OK", out


def dial(callee, **kw):
    intent = new_intent(callee, **kw)
    write_intent(intent)
    status, body = post(E["VERIFY_DIAL_V6"], "/campaign-dial", dr.canonical_body(intent, int(time.time())))
    return intent, status, body


def wait_for(fn, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.5)
    return False


def stub_invites(to=None):
    return count(E["VERIFY_STUB_LOG"], r"mod_dptools.c:\d+ PSTN_STUB_INVITE from \S+ to " + (re.escape(to) if to else r"\S+"))


def events(key):
    out = redis(E["VERIFY_REDIS_READER"], "xrange", "campaign:events:fs", "-", "+").splitlines()
    entries, current = [], None
    for line in out:
        if re.fullmatch(r"\d+-\d+", line):
            current = []
            entries.append(current)
        elif current is not None:
            current.append(line)
    found = [dict(zip(e[::2], e[1::2])) for e in entries]
    return [e for e in found if e.get("key") == key]


# ── raw SIP ───────────────────────────────────────────────────────────────
class Udp:
    """A UDP socket that remembers where it sends."""

    def __init__(self, dst, bind_ip="127.0.0.1"):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind_ip, 0))
        self.dst = dst

    def port(self):
        return self.sock.getsockname()[1]


def sip_send(sock, method_line, headers, body=""):
    sock.sock.sendto(("\r\n".join([method_line, *headers, f"Content-Length: {len(body)}"]) + "\r\n\r\n" + body).encode(), sock.dst)


def sip_final(sock, timeout=4):
    end, last = time.time() + timeout, None
    while time.time() < end:
        sock.sock.settimeout(max(0.05, end - time.time()))
        try:
            data, _ = sock.sock.recvfrom(65535)
        except socket.timeout:
            break
        text = data.decode(errors="replace")
        m = re.match(r"SIP/2.0 (\d+) ", text)
        if m:
            last = (int(m.group(1)), text)
            if last[0] >= 200:
                return last
    return last


SDP = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 49170 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\n"


def invite(sock, ruri_user, from_user, from_domain, extra=(), auth=None, call_id=None, cseq=1, sdp=True, bind_ip="127.0.0.1"):
    port = sock.port()
    call_id = call_id or str(uuid.uuid4())
    host, dport = sock.dst
    headers = [f"Via: SIP/2.0/UDP {bind_ip}:{port};branch=z9hG4bK{uuid.uuid4().hex[:10]}", "Max-Forwards: 70",
               f"From: <sip:{from_user}@{from_domain}>;tag={uuid.uuid4().hex[:8]}", f"To: <sip:{ruri_user}@{host}>",
               f"Call-ID: {call_id}", f"CSeq: {cseq} INVITE", f"Contact: <sip:{from_user}@{bind_ip}:{port}>", *extra]
    if auth:
        headers.append(auth)
    if sdp:
        headers.append("Content-Type: application/sdp")
    sip_send(sock, f"INVITE sip:{ruri_user}@{host}:{dport} SIP/2.0", headers, SDP if sdp else "")
    return call_id


udp = Udp


def attempt(ruri_user, *, token=None, pai=None, user="fs-egress", password=None, authenticate=True, extra=()):
    password = E["VERIFY_FS_EGRESS_PASSWORD"] if password is None else password
    s = udp(KAM)
    headers = list(extra) + ([f"X-Egress-Token: {token}"] if token else []) + ([f"P-Asserted-Identity: <sip:{pai}@127.0.0.1>"] if pai else [])
    call_id = invite(s, ruri_user, user, FROM_DOMAIN, headers)
    r = sip_final(s)
    if r and r[0] in (401, 407) and authenticate:
        realm = re.search(r'realm="([^"]+)"', r[1]).group(1)
        nonce = re.search(r'nonce="([^"]+)"', r[1]).group(1)
        uri = f"sip:{ruri_user}@{KAM[0]}:{KAM[1]}"
        ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"INVITE:{uri}".encode()).hexdigest()
        resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        auth = f'{"Authorization" if r[0] == 401 else "Proxy-Authorization"}: Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{resp}", algorithm=MD5'
        invite(s, ruri_user, user, FROM_DOMAIN, headers, auth=auth, call_id=call_id, cseq=2)
        r = sip_final(s)
    return s, r


def hang_up(s, r):
    if not r or r[0] != 200:
        return
    text = r[1]
    to = re.search(r"^To: (.*)$", text, re.M).group(1).strip()
    frm = re.search(r"^From: (.*)$", text, re.M).group(1).strip()
    call_id = re.search(r"^Call-ID: (.*)$", text, re.M).group(1).strip()
    contact = re.search(r"^Contact: <([^>]+)>", text, re.M).group(1)
    routes = re.findall(r"^Record-Route: (.*)$", text, re.M)
    route = [("Route: " + ", ".join(routes))] if routes else []
    port = s.port()
    for method, cseq in (("ACK", 2), ("BYE", 3)):
        sip_send(s, f"{method} {contact} SIP/2.0", [f"Via: SIP/2.0/UDP 127.0.0.1:{port};branch=z9hG4bK{uuid.uuid4().hex[:10]}",
                 "Max-Forwards: 70", f"From: {frm}", f"To: {to}", f"Call-ID: {call_id}", f"CSeq: {cseq} {method}", *route])
        if method == "BYE":
            sip_final(s, 2)


def token(key, callee, caller, max_call_s=60, exp=None, tenant=TENANT):
    return dr.egress_token(key=key, callee=callee, caller_id=caller, tenant_id=tenant, max_call_s=max_call_s,
                           exp=exp or int(time.time()) + 60, secret=EGRESS_SECRET)


def kam_count(pattern):
    return count(E["VERIFY_KAM_LOG"], pattern)


def stop_node(name):
    subprocess.run([E["VERIFY_FS_CLI"], "-H", "127.0.0.1", "-P", NODES[name]["esl"], "-p", E["VERIFY_ESL_PASSWORD"], "-x", "fsctl shutdown"],
                   capture_output=True, text=True)


def node_flag(name):
    host, port = NODES[name]["sip"]
    m = re.search(rf"URI: sip:{re.escape(host)}:{port}\s+FLAGS: (\w+)", kam("dispatcher.list"))
    return m.group(1) if m else None


# ── 1. the 40 s limit starts now so it can run alongside the rest ─────────
limit_intent, status, body = dial("+15551230040", max_call_s=40)
check("a signed dial from Python is accepted (Kamailio HMAC = Python HMAC)", (status, body) == (200, '{"result":"accepted"}'), (status, body))
check("the first dial reaches the carrier through Kamailio's egress route", wait_for(lambda: kam_count("EGRESS_ADMIT") >= 1, 15))

# ── 2. dial handler refusals, with no server transaction created ──────────
good_intent = new_intent("+15551230050")
good_body = dr.canonical_body(good_intent, int(time.time()))
d = json.loads(good_body)
compact = lambda obj: json.dumps(obj, separators=(",", ":")).encode()
stale = compact({**d, "ts": int(time.time()) - 120})
before_tx, before_dials = server_transactions(), kam_count("DIAL_SENT")
cases = [
    ("bad signature -> 401", post(E["VERIFY_DIAL_V6"], "/campaign-dial", good_body, sig="AAAA")[0], 401),
    ("signed with the wrong secret -> 401", post(E["VERIFY_DIAL_V6"], "/campaign-dial", good_body, sig=dr._mac(b"nope", good_body))[0], 401),
    ("stale timestamp -> 401", post(E["VERIFY_DIAL_V6"], "/campaign-dial", stale)[0], 401),
    ("a source outside the allow-list -> 403", post(E["VERIFY_DIAL_V4"], "/campaign-dial", good_body)[0], 403),
    ("extra key in a signed body -> 400", post(E["VERIFY_DIAL_V6"], "/campaign-dial", compact({**d, "x": "1"}))[0], 400),
    ("reordered keys in a signed body -> 400", post(E["VERIFY_DIAL_V6"], "/campaign-dial", compact(dict(reversed(list(d.items())))))[0], 400),
    ("retyped ts in a signed body -> 400", post(E["VERIFY_DIAL_V6"], "/campaign-dial", good_body.replace(f'"ts":{d["ts"]}'.encode(), f'"ts":"{d["ts"]}"'.encode()))[0], 400),
    ("CRLF in the callee -> 400", post(E["VERIFY_DIAL_V6"], "/campaign-dial", compact({**d, "callee": "+1555123\r\nX-Evil: 1"}))[0], 400),
    ("oversize body -> 400", post(E["VERIFY_DIAL_V6"], "/campaign-dial", compact({**d, "pad": "x" * 2000}))[0], 400),
    ("malformed JSON -> 400", post(E["VERIFY_DIAL_V6"], "/campaign-dial", b"{not json")[0], 400),
    ("GET -> 405", post(E["VERIFY_DIAL_V6"], "/campaign-dial", b"", method="GET")[0], 405),
    ("another path -> 404", post(E["VERIFY_DIAL_V6"], "/campaign-dial/x", good_body)[0], 404),
    ("/RPC core.ps -> 404", post(E["VERIFY_DIAL_V6"], "/RPC", compact({"jsonrpc": "2.0", "method": "core.ps", "id": 1}))[0], 404),
    ("/RPC GET -> 404", post(E["VERIFY_DIAL_V6"], "/RPC", b"", method="GET")[0], 404),
    ("/ps -> 404", post(E["VERIFY_DIAL_V6"], "/ps", b"ps")[0], 404),
]
for name, got, want in cases:
    check(name, got == want, got)
check("the refused requests created no transaction and sent no INVITE",
      server_transactions() == before_tx and kam_count("DIAL_SENT") == before_dials, (before_tx, server_transactions()))

lan = E.get("VERIFY_LAN_IP", "")
port = urlparse(E["VERIFY_DIAL_V6"]).port
if lan:
    s = socket.socket()
    s.settimeout(2)
    check(f"the dial port is closed on {lan} (not reachable from another host)", s.connect_ex((lan, port)) != 0)
else:
    check("the dial port is closed on the LAN address", False, "no LAN address; set VERIFY_LAN_IP")

# ── 3. campaign headers are stripped before routing ───────────────────────
probe_headers = ["X-Call-Direction: outbound", "X-Idempotency-Key: spoof", "X-Callee: +15550000001", "x-caller-id: +15550000002",
                 "X-Agent-DID: +15550000003", "X-Campaign-Id: c", "X-Contact-Id: c"]
s = udp(KAM)
invite(s, "5000", "1001", DOMAIN, probe_headers)
sip_final(s)
time.sleep(1)
probe = re.findall(r"RIG_PROBE dir=\[(.*?)\] idem=\[(.*?)\] callee=\[(.*?)\] cid=\[(.*?)\]", "".join(read(p) for p in node_logs()))
check("headers sent to a 500x extension through Kamailio reach FreeSWITCH stripped", probe and set(probe[-1]) == {""}, probe[-1:] )
s = udp(NODES["A"]["sip"])
invite(s, "5000", "campaigns", DOMAIN, probe_headers)
sip_final(s)
time.sleep(1)
probe = re.findall(r"RIG_PROBE dir=\[(.*?)\] idem=\[(.*?)\] callee=\[(.*?)\] cid=\[(.*?)\]", read(NODES["A"]["log"]))
check("control: the same headers sent straight to FreeSWITCH arrive (so the strip is Kamailio's)", probe and probe[-1][0] == "outbound", probe[-1:])

# ── 4. spoofed trigger INVITEs straight to a FreeSWITCH node ──────────────
def trigger(node, key, extra=(), bind_ip="127.0.0.1"):
    s = udp(NODES[node]["sip"], bind_ip)
    invite(s, "outbound-trigger", "campaigns", DOMAIN, ["X-Call-Direction: outbound", f"X-Idempotency-Key: {key}", *extra], sdp=False, bind_ip=bind_ip)
    r = sip_final(s)
    return r and r[0], (re.search(r"X-Campaign-Result: (\S+)", r[1]).group(1) if r and "X-Campaign-Result" in r[1] else None)


stub_before = stub_invites()
check("a trigger from outside the kamailio acl is refused with 403", bool(lan) and trigger("A", f"{uuid.uuid4()}_1", bind_ip=lan)[0] == 403)
check("a trigger with no intent is declined as rejected and dials nothing",
      trigger("A", f"{uuid.uuid4()}_1", ["X-Callee: +15557770001", "X-Caller-Id: +15557770002", "X-Egress-Token: forged"]) == (603, "rejected"))
forged = new_intent("+15551230060")
write_intent(replace(forged, egress_token=forged.egress_token.replace(TENANT, str(uuid.uuid4()), 1)))
check("an intent whose token names another tenant is rejected", trigger("A", forged.key) == (603, "rejected"))
mac_bad = new_intent("+15551230062")
write_intent(replace(mac_bad, egress_token=mac_bad.egress_token[:-4] + "AAAA"))
check("an intent whose token MAC is wrong is accepted here (only Kamailio holds the secret)", trigger("A", mac_bad.key) == (603, "accepted"))
time.sleep(3)
check("none of the spoofed triggers reached the carrier (Kamailio refused the forged MAC)", stub_invites() == stub_before and stub_invites("+15551230062") == 0, stub_invites() - stub_before)
real = new_intent("+15551230061")
write_intent(real)
check("a trigger with forged X-Callee headers is accepted for the intent only",
      trigger("A", real.key, ["X-Callee: +15557770009", "X-Caller-Id: +15557770008"]) == (603, "accepted"))
check("the intent's callee is dialled, the header's is not",
      wait_for(lambda: stub_invites("+15551230061") == 1) and stub_invites("+15557770009") == 0)
check("replaying the trigger finds no intent and is rejected", trigger("A", real.key) == (603, "rejected"))
write_intent(real)
check("a fresh intent for an already-claimed key is a duplicate and dials nothing",
      trigger("A", real.key) == (603, "duplicate") and stub_invites("+15551230061") == 1)

# ── 5. PSTN egress token matrix ───────────────────────────────────────────
callee, other = "+15551239001", "+15551239002"
st0, admit0 = stub_invites(), kam_count("EGRESS_ADMIT")
rows = {}


def row(name, ruri=callee, **kw):
    s, r = attempt(ruri, **kw)
    rows[name] = r and r[0]
    return s, r


key = f"{uuid.uuid4()}_1"
row("missing token", pai=CALLER_ID)
row("garbage token", token="garbage", pai=CALLER_ID)
row("token for another callee", ruri=other, token=token(f"{uuid.uuid4()}_1", callee, CALLER_ID), pai=CALLER_ID)
row("PAI differs from the token's caller id", token=token(f"{uuid.uuid4()}_1", callee, CALLER_ID), pai="+15559990002")
row("no PAI", token=token(f"{uuid.uuid4()}_1", callee, CALLER_ID))
row("token minted for another caller id", token=token(f"{uuid.uuid4()}_1", callee, "+15559990002"), pai=CALLER_ID)
row("expired token", token=token(f"{uuid.uuid4()}_1", callee, CALLER_ID, exp=int(time.time()) - 5), pai=CALLER_ID)
row("tampered max_call_s", token=re.sub(r"\.60\.", ".7200.", token(f"{uuid.uuid4()}_1", callee, CALLER_ID), count=1), pai=CALLER_ID)
row("wrong password", token=token(f"{uuid.uuid4()}_1", callee, CALLER_ID), pai=CALLER_ID, password="wrong")
row("no credentials", token=token(f"{uuid.uuid4()}_1", callee, CALLER_ID), pai=CALLER_ID, authenticate=False)
check("every bad egress token or credential is refused", all(v in (401, 403, 407) for v in rows.values()), rows)
time.sleep(1)
check("the carrier saw none of them", stub_invites() == st0 and kam_count("EGRESS_ADMIT") == admit0, (stub_invites() - st0, kam_count("EGRESS_ADMIT") - admit0))
good = token(key, callee, CALLER_ID, max_call_s=60)
s, r = row("valid", token=good, pai=CALLER_ID)
check("a valid token with matching PAI is admitted to the carrier", r and r[0] == 200 and wait_for(lambda: stub_invites(callee) == 1), r and r[0])
hang_up(s, r)
time.sleep(1)
check("the same token is refused the second time", attempt(callee, token=good, pai=CALLER_ID)[1][0] == 403)
check("the same token is refused on another callee", attempt(other, token=good, pai=CALLER_ID)[1][0] == 403)
vector = json.load(open(os.path.join(E["VERIFY_REPO"], "services/campaigns/tests/vectors/egress_token.json")))[0]
s, r = attempt(vector["callee"], token=vector["token"], pai=vector["caller_id"])
check("Kamailio admits the Python test-vector token (same MAC bytes)", r and r[0] == 200, r and r[0])
hang_up(s, r)

# ── 6. softphone and transfer legs are not routed to the carrier ──────────
st0 = stub_invites()
user, pw = E["VERIFY_SOFTPHONE"].split(":")
s, r = attempt("+15551239003", user=user, password=pw, token=token(f"{uuid.uuid4()}_1", "+15551239003", CALLER_ID), pai=CALLER_ID)
check("a softphone subscriber calling an E.164 number is not routed to the carrier", not (r and r[0] == 200), r and r[0])
s, r = attempt("+15551239005", user="fs-egress", password=pw, token=token(f"{uuid.uuid4()}_1", "+15551239005", CALLER_ID), pai=CALLER_ID)
check("softphone credentials presented as fs-egress are refused", not (r and r[0] == 200), r and r[0])
s = udp(KAM)
invite(s, "+15551239004", "freeswitch-transfer", FROM_DOMAIN)
r = sip_final(s)
check("an unauthenticated transfer-style INVITE is challenged, not routed", r and r[0] in (401, 407), r and r[0])
time.sleep(1)
check("neither reached the carrier", stub_invites() == st0, stub_invites() - st0)
s, r = attempt("+15551231001", token=token(f"{uuid.uuid4()}_1", "+15551231001", CALLER_ID), pai=CALLER_ID)
check("a callee containing 1001 is egress, not the softphone extension", r and r[0] == 200 and wait_for(lambda: stub_invites("+15551231001") == 1), r and r[0])
hang_up(s, r)

# ── 7. ten calls: outbound metadata with the callee as ani ────────────────
callees = [f"+1555123{2000 + i}" for i in range(10)]
sent = [dial(c) for c in callees]
check("ten dials are accepted", all(st == 200 for _, st, _ in sent), [st for _, st, _ in sent])
forks = lambda: "".join(read(p) for p in node_logs())
check("each of the ten calls starts a conversation with direction=outbound, did=agent DID and ani=callee",
      wait_for(lambda: all(re.search(rf'RIG_FORK \S+ start \S+ mono 16000 {{"did":"{re.escape(CALLER_ID)}","ani":"{re.escape(c)}","direction":"outbound"', forks()) for c in callees), 40),
      "missing: " + ",".join(c for c in callees if c not in forks()))
check("the ten calls were spread over both nodes by the dispatcher", all(count(n["log"], "RIG_FORK") > 0 for n in NODES.values()))

# ── 8. the 40 s agent limit ends the call with ALLOTTED_TIMEOUT ───────────
wait_for(lambda: any(e.get("kind") == "hangup" for e in events(limit_intent.key)), 70)
ev = events(limit_intent.key)
hang = next((e for e in ev if e.get("kind") == "hangup"), {})
check("an answered event and a hangup event reached campaign:events:fs with the tenant", any(e.get("kind") == "answered" for e in ev) and hang.get("tenant_id") == TENANT, ev)
check("the call hung up about 40 s after answer with ALLOTTED_TIMEOUT",
      hang.get("cause") == "ALLOTTED_TIMEOUT" and 38000 <= int(hang.get("billable_ms", 0)) <= 43000, hang)

# ── 9. node failure ───────────────────────────────────────────────────────
time.sleep(12)   # let the ten calls finish so the dispatcher's view is the only thing changing
names = list(NODES)
down, up = names[1], names[0]
stop_node(down)
check(f"node {down} is marked down by the dispatcher", wait_for(lambda: (node_flag(down) or "A")[0] != "A", 40), node_flag(down))
sent = [dial(f"+1555123{3000 + i}") for i in range(4)]
check("with one node down every dial is accepted", all(st == 200 for _, st, _ in sent), [st for _, st, _ in sent])
host_up = NODES[up]["sip"]
last = re.findall(r"DIAL_SENT key=\S+ node=sip:(\S+)", read(E["VERIFY_KAM_LOG"]))[-4:]
check(f"the last four dials were routed to {up} only", set(last) == {f"{host_up[0]}:{host_up[1]}"}, last)
stop_node(up)
check(f"node {up} is marked down by the dispatcher", wait_for(lambda: (node_flag(up) or "A")[0] != "A", 40), node_flag(up))
intent, status, body = dial("+15551234000")
check("with both nodes down a dial answers 503 no_node", (status, body) == (503, '{"result":"no_node"}'), (status, body))

failed = [name for ok, name in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
PY
