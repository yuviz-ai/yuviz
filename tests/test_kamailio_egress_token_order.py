"""The Kamailio egress-token check and the Python mint must agree on field order and MAC encoding."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_TPL = (_REPO / "scripts/kamailio/kamailio.cfg.tpl").read_text()
_VECTORS = json.loads((_REPO / "services/campaigns/tests/vectors/egress_token.json").read_text())

_TERM = {"$var(key)": "key", "$rU": "callee", "$var(cid)": "caller_id", "$var(tenant)": "tenant_id",
         "$var(max)": "max_call_s", "$var(exp)": "exp"}


def _egress_route() -> str:
    match = re.search(r"^route\[PSTN_EGRESS\] \{\n(.*?)^\}", _TPL, re.M | re.S)
    assert match, "route[PSTN_EGRESS] not found"
    return match.group(1)


def _kamailio_message_fields() -> list[str]:
    expr = re.search(r"\$var\(msg\) = (.*);", _egress_route())
    assert expr, "found no $var(msg) expression"
    terms = [t.strip() for t in expr.group(1).split("+")]
    assert terms[1::2] == ['"|"'] * (len(terms) // 2), terms
    return [_TERM[t] for t in terms[0::2]]


def _kamailio_token_fields() -> list[str]:
    picks = dict(re.findall(r"\$var\((\w+)\) = \$\(var\(tok\)\{s\.select,(\d),\.\}\);", _egress_route()))
    assert len(picks) == 6, picks
    by_index = {int(i): _TERM.get(f"$var({name})", name) for name, i in picks.items()}
    return [by_index[i] for i in range(6)]


def test_kamailio_mac_input_order_is_the_documented_one():
    assert _kamailio_message_fields() == ["key", "callee", "caller_id", "tenant_id", "max_call_s", "exp"]


def test_kamailio_token_field_order_matches_the_python_mint():
    assert _kamailio_token_fields() == ["key", "exp", "tenant_id", "caller_id", "max_call_s", "mac"]
    assert len(_VECTORS) > 0
    for v in _VECTORS:
        parsed = dict(zip(_kamailio_token_fields(), v["token"].split(".")))
        parsed.pop("mac")
        assert parsed == {"key": v["key"], "exp": str(v["exp"]), "tenant_id": v["tenant_id"],
                          "caller_id": v["caller_id"], "max_call_s": str(v["max_call_s"])}


@pytest.mark.parametrize("vector", _VECTORS)
def test_kamailio_message_order_reproduces_the_python_vector_mac(vector):
    message = "|".join(str(vector[f]) for f in _kamailio_message_fields())
    digest = hmac.new(vector["secret"].encode(), message.encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == vector["token"].rsplit(".", 1)[1]


def test_kamailio_compares_exp_numerically_and_the_pai_user_to_the_caller_id():
    route = _egress_route()
    assert "$(var(exp){s.int}) < $Ts" in route
    assert "$(ai{uri.user}) == $var(cid)" in route
    assert 'sl_send_reply("503"' in route.split("$var(tok)")[0]
