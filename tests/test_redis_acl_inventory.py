"""The ACL template grants every client exactly what its source uses; a new key prefix must fail here."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("redis_acl_inventory", _REPO / "scripts/redis_acl_inventory.py")
inv_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inv_mod)

_TEMPLATE = (_REPO / "deployment/docker/redis/users.acl.tpl").read_text()
_EXPECTED_CLIENTS = {"gateway", "config", "did", "knowledge", "conversation", "telephony", "campaigns"}


def test_one_entry_per_client():
    inv = inv_mod.inventory()
    assert set(inv) == _EXPECTED_CLIENTS
    assert len(inv) > 0


def test_every_client_with_redis_use_was_found():
    inv = inv_mod.inventory()
    users = [n for n, e in inv.items() if e["commands"]]
    assert len(users) >= 5, f"scan found too little redis use: {users}"
    assert "did:*" in inv["gateway"]["keys"] and "get" in inv["gateway"]["commands"]


def test_template_grants_everything_the_inventory_finds():
    assert inv_mod.uncovered(inv_mod.inventory(), _TEMPLATE) == []


def test_unlisted_key_prefix_fails(tmp_path):
    rogue = tmp_path / "rogue.py"
    rogue.write_text(
        "import redis.asyncio as redis\n"
        "async def f(client, x):\n"
        "    await client.get(f'rogue:{x}')\n"
        "    await client.exists('rogue:stream')\n"
    )
    inv = inv_mod.inventory(clients={"conversation": [str(rogue)]})
    assert inv["conversation"]["keys"] == ["rogue:*", "rogue:stream"]
    missing = inv_mod.uncovered(inv, _TEMPLATE)
    assert ("conversation", "keys", "rogue:*") in missing
    assert ("conversation", "commands", "exists") in missing
