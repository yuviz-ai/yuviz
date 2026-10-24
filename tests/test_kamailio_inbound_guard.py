"""Campaign headers are stripped from every request before routing, and the FreeSWITCH-origin loop guards
accept any dispatcher node, not just :5080 on this host."""
from __future__ import annotations

import re
from pathlib import Path

_TPL = (Path(__file__).resolve().parents[1] / "scripts/kamailio/kamailio.cfg.tpl").read_text()

_CAMPAIGN_HEADERS = ["X-Call-Direction", "X-Idempotency-Key", "X-Campaign-Id", "X-Contact-Id", "X-Callee",
                     "X-Caller-Id", "X-Agent-DID"]

# Line 794 of the template before this change: it sits inside a /* ... */ block and must stay untouched.
_COMMENTED_GUARD = '''    if ($si == "__LAN_IP__" && $sp == "5080") { 
        xlog("L_INFO", "REAL Loopback: Call returned from FS ($si:$sp) for $rU\\n");'''


def test_strip_regex_covers_all_seven_campaign_headers():
    pattern = re.search(r'remove_hf_re\("(.+?)"\);', _TPL).group(1)
    assert len(_CAMPAIGN_HEADERS) == 7
    for header in _CAMPAIGN_HEADERS:
        assert re.search(pattern, header), header
    assert not re.search(pattern, "X-Callee-Extra") and not re.search(pattern, "X-Egress-Token")


def test_strip_runs_right_after_request_init_before_any_routing():
    after_init = _TPL.split("\troute(REQINIT);\n", 1)[1]
    assert re.match(r"\n#!ifdef WITH_CAMPAIGN_DIAL\n(\t#[^\n]*\n)?\tremove_hf_re\(", after_init)
    assert after_init.index("remove_hf_re(") < after_init.index("route(NATDETECT)")
    assert after_init.index("remove_hf_re(") < after_init.index("t_relay")


def test_both_live_loop_guards_use_the_dispatcher_list_when_campaign_dial_is_on():
    guarded = re.findall(
        r'#!ifdef WITH_CAMPAIGN_DIAL\n\s+if \(ds_is_from_list\("1"\)\) \{\n#!else\n(\s+if \(.*\) \{)\n#!endif', _TPL)
    assert [g.strip() for g in guarded] == ['if (src_ip == __LAN_IP__ && src_port == 5080) {',
                                           'if ($si == "__LAN_IP__" && $sp == "5080") {']


def test_the_commented_out_guard_is_untouched():
    assert _COMMENTED_GUARD in _TPL


def test_extension_and_did_matches_are_anchored_so_a_callee_number_cannot_contain_them():
    assert not re.search(r'\$rU =~ "(?!\^)[^"]*(100\[0-2\]|500\[0-9\])', _TPL)
    assert '$rU =~ "^500[0-9]$"' in _TPL and '$rU =~ "^100[0-2]$"' in _TPL
