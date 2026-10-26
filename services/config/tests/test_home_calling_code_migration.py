"""schema.sql derives each existing tenant's home_calling_code from its own numbers (#168).

A flat default of '1' would turn every +91 destination of an Indian tenant into an
"international" one and break it on upgrade. Each test builds a database the way it looks
before this release (no home_calling_code column, live data), then applies schema.sql as an
upgrade does.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
import warnings
from pathlib import Path

import asyncpg
import pytest

from libs.config_sdk.calling_codes import CALLING_CODES
from libs.config_sdk.dial_targets import TransferPolicy, transfer_policy_problem
from services.config.tenants import transfer_policy

REPO = Path(__file__).resolve().parents[3]
SCHEMA = REPO / "database" / "schema.sql"
_BASE = os.environ["POSTGRES_DSN"].rsplit("/", 1)[0]


def _psql(database: str, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["psql", f"{_BASE}/{database}", "-q", "-v", "ON_ERROR_STOP=1", *args],
        check=True, capture_output=True, timeout=timeout,
    )


@pytest.fixture(scope="module")
def scratch_db():
    name = f"home_code_scratch_{uuid.uuid4().hex[:8]}"
    _psql("postgres", "-c", f"CREATE DATABASE {name}")
    _psql(name, "-f", str(SCHEMA))
    yield name
    try:
        _psql("postgres", "-c", f"DROP DATABASE IF EXISTS {name}", timeout=5)
    except subprocess.TimeoutExpired:
        warnings.warn(f"scratch database {name} was not dropped: DROP DATABASE did not return in 5s")


@pytest.fixture(scope="module")
def rls_db():
    """CI-style database (every schema file, then rls.sql with yuviz_app) whose tables
    are owned by a role that is neither superuser nor BYPASSRLS, as on a managed Postgres."""
    name = f"home_code_rls_{uuid.uuid4().hex[:8]}"
    owner = f"home_code_owner_{uuid.uuid4().hex[:8]}"
    _psql("postgres", "-c", f"CREATE DATABASE {name}")
    for sql in ("schema", "knowledge_schema", "telephony_schema", "clinic_schema"):
        _psql(name, "-f", str(REPO / "database" / f"{sql}.sql"))
    _psql(name, "-v", "yuviz_app_password=testpw", "-f", str(REPO / "database" / "rls.sql"))
    _psql("postgres", "-c", f"CREATE ROLE {owner} NOSUPERUSER NOBYPASSRLS")
    _psql(
        name, "-c", f"GRANT ALL ON SCHEMA public TO {owner}",
        "-c", f"""DO $$ DECLARE t text; BEGIN
          FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
            EXECUTE format('ALTER TABLE %I OWNER TO {owner}', t);
          END LOOP;
          FOR t IN SELECT p.oid::regprocedure::text FROM pg_proc p
                    WHERE p.pronamespace = 'public'::regnamespace AND NOT EXISTS (
                      SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e') LOOP
            EXECUTE format('ALTER FUNCTION %s OWNER TO {owner}', t);
          END LOOP; END $$""",
    )
    yield name, owner
    _psql("postgres", "-c", f"DROP DATABASE IF EXISTS {name}", "-c", f"DROP ROLE IF EXISTS {owner}", timeout=10)


@pytest.fixture
async def conn(scratch_db):
    """Connection to a database holding the pre-release shape: no home_calling_code."""
    c = await asyncpg.connect(f"{_BASE}/{scratch_db}")
    await c.execute("TRUNCATE tenants CASCADE")
    await c.execute("DELETE FROM audit_log WHERE entity_type = 'tenant_home_calling_code'")
    await c.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS home_calling_code")
    yield c
    await c.close()


def _upgrade(scratch_db: str) -> str:
    return _psql(scratch_db, "-f", str(SCHEMA)).stderr.decode()


async def _tenant(conn, dids: tuple[str, ...] = (), destinations: tuple[str, ...] = ()) -> uuid.UUID:
    slug = f"t-{uuid.uuid4().hex[:8]}"
    tenant = await conn.fetchval("INSERT INTO tenants (name, slug) VALUES ($1, $1) RETURNING id", slug)
    for did in dids:
        await conn.execute("INSERT INTO phone_numbers (did, tenant_id) VALUES ($1, $2)", did, tenant)
    for i, destination in enumerate(destinations):
        await conn.execute(
            "INSERT INTO agents (tenant_id, slug, name, transfer_type, transfer_destination) "
            "VALUES ($1, $2, 'a', 'cold', $3)", tenant, f"a{i}", destination,
        )
    return tenant


async def _dial_flow(conn, tenant, destination: str) -> None:
    graph = json.dumps({"nodes": [
        {"id": "d", "type": "dial", "data": {"destination": destination}}, {"id": "s", "type": "start"},
    ]})
    await conn.execute(
        "INSERT INTO call_flows (tenant_id, slug, name, graph) VALUES ($1, $2, 'f', $3::jsonb)",
        tenant, f"f-{uuid.uuid4().hex[:6]}", graph,
    )


async def _workflow_transfer(conn, tenant, destination: str) -> None:
    workflow = json.dumps({"nodes": [{"id": "t", "type": "transfer", "data": {"transfer_destination": destination}}]})
    await conn.execute(
        "INSERT INTO agents (tenant_id, slug, name, workflow) VALUES ($1, $2, 'w', $3::jsonb)",
        tenant, f"w-{uuid.uuid4().hex[:6]}", workflow,
    )


async def _audit_rows(conn, tenant) -> list[asyncpg.Record]:
    return await conn.fetch(
        "SELECT new_value FROM audit_log WHERE entity_type = 'tenant_home_calling_code' AND entity_id = $1", tenant,
    )


async def _home(conn, tenant) -> str:
    return await conn.fetchval("SELECT home_calling_code FROM tenants WHERE id = $1", tenant)


async def test_indian_tenant_keeps_its_plus91_destinations(scratch_db, conn):
    destinations = ("+918012345678", "00918098765432", "1001", "09876543210", "9005551234")
    tenant = await _tenant(conn, dids=("918065354620", "918065354621"), destinations=destinations)
    _upgrade(scratch_db)
    row = await conn.fetchrow("SELECT * FROM tenants WHERE id = $1", tenant)
    assert row["home_calling_code"] == "91"
    for destination in destinations:
        assert transfer_policy_problem(destination, transfer_policy(dict(row))) is None, destination
    # The same destinations under the old flat default are what broke.
    assert transfer_policy_problem("+918012345678", TransferPolicy()) is not None


async def test_home_code_votes_come_from_explicit_destinations(scratch_db, conn):
    from_dests = await _tenant(conn, destinations=("+442071234567", "0044207123456", "+918012345678", "+442071234568"))
    only_extensions = await _tenant(conn, destinations=("1001", "sip:desk@pbx.example.com", "09876543210"))
    _upgrade(scratch_db)
    assert await _home(conn, from_dests) == "44"
    assert await _home(conn, only_extensions) == "1"


async def test_dids_never_vote_in_any_form(scratch_db, conn):
    # 18001021234 is Indian toll-free; read as NANP it outvoted the one +91 number. The upgrade
    # normalizes every DID to plain digits before the derivation, so none of these can vote.
    toll_free = await _tenant(
        conn, dids=("18001021234", "18001021235", "+918065354824"), destinations=("+918012345678",),
    )
    explicit_dids_only = await _tenant(conn, dids=("+918065354620", "+918065354621"))
    national = await _tenant(conn, dids=("9876543210", "4165550177", "(416) 555-0178", "7000000000"))
    _upgrade(scratch_db)
    assert await _home(conn, toll_free) == "91"
    assert await _home(conn, explicit_dids_only) == "1"
    assert await _home(conn, national) == "1"
    assert len(await _audit_rows(conn, explicit_dids_only)) == 1


async def test_call_flow_dial_and_workflow_transfer_destinations_are_evidence(scratch_db, conn):
    dial_only = await _tenant(conn)
    await _dial_flow(conn, dial_only, "+919876543211")
    workflow_only = await _tenant(conn)
    await _workflow_transfer(conn, workflow_only, "+919876543210")
    templated = await _tenant(conn)
    await _workflow_transfer(conn, templated, "{{ caller_number }}")
    await _dial_flow(conn, templated, "1001")
    _upgrade(scratch_db)
    assert await _home(conn, dial_only) == "91"
    assert await _home(conn, workflow_only) == "91"
    assert await _home(conn, templated) == "1"
    assert len(await _audit_rows(conn, templated)) == 1


async def _caller_id_agent(conn, tenant, outbound_caller_id: str, *, deleted: bool = False) -> None:
    await conn.execute(
        "INSERT INTO agents (tenant_id, slug, name, outbound_caller_id, deleted_at) "
        "VALUES ($1, $2, 'c', $3, CASE WHEN $4 THEN now() END)",
        tenant, f"c-{uuid.uuid4().hex[:6]}", outbound_caller_id, deleted,
    )


async def test_outbound_caller_id_is_evidence(scratch_db, conn):
    campaign_only = await _tenant(conn)
    await _caller_id_agent(conn, campaign_only, "+919999900000")
    # One vote per distinct number: the same +1 number as a destination and a caller id, against
    # one +91 caller id, is a tie.
    repeated = await _tenant(conn, destinations=("+14155550100",))
    await _caller_id_agent(conn, repeated, "+14155550100")
    await _caller_id_agent(conn, repeated, "+918012345678")
    # Plain digits cannot name a country, and a deleted agent's caller id no longer counts.
    no_evidence = await _tenant(conn)
    await _caller_id_agent(conn, no_evidence, "919999900001")
    await _caller_id_agent(conn, no_evidence, "+919999900002", deleted=True)
    _upgrade(scratch_db)
    assert await _home(conn, campaign_only) == "91"
    assert await _home(conn, repeated) == "1"
    assert len(await _audit_rows(conn, repeated)) == 1
    assert await _home(conn, no_evidence) == "1"
    assert len(await _audit_rows(conn, no_evidence)) == 1


async def test_a_number_repeated_across_sources_is_one_vote(scratch_db, conn):
    # One +1 number against the same +91 number in three agents and a flow: one vote each, a tie.
    repeated = await _tenant(conn, destinations=("+14155550100", "+918012345678", "00918012345678", "+918012345678"))
    await _dial_flow(conn, repeated, "+918012345678")
    # Distinct numbers each count: one +91 number against two different +98 numbers.
    distinct = await _tenant(conn, destinations=("+918065354620", "+989121234567", "+989121234568"))
    notices = _upgrade(scratch_db)
    assert await _home(conn, repeated) == "1"
    assert str(repeated) in notices
    assert await _home(conn, distinct) == "98"


async def test_a_tie_or_no_evidence_keeps_the_default_names_the_tenant_and_writes_an_audit_row(scratch_db, conn):
    tied = await _tenant(conn, destinations=("+918065354622", "+442071234567"))
    no_data = await _tenant(conn, dids=("918065354624",))
    decided = await _tenant(conn, destinations=("+442071234567", "+918012345678", "+918012345679"))
    notices = _upgrade(scratch_db)
    assert await _home(conn, tied) == "1"
    assert await _home(conn, no_data) == "1"
    assert str(tied) in notices and str(no_data) in notices
    assert await _home(conn, decided) == "91"
    assert str(decided) not in notices
    [tie_row] = await _audit_rows(conn, tied)
    assert sorted(json.loads(tie_row["new_value"])["candidates"]) == ["44", "91"]
    [empty_row] = await _audit_rows(conn, no_data)
    assert json.loads(empty_row["new_value"])["candidates"] is None
    assert await _audit_rows(conn, decided) == []


async def test_reapplying_schema_keeps_a_code_set_since(scratch_db, conn):
    tenant = await _tenant(conn, destinations=("+918065354620",))
    _upgrade(scratch_db)
    await conn.execute("UPDATE tenants SET home_calling_code = '44' WHERE id = $1", tenant)
    _upgrade(scratch_db)
    assert await _home(conn, tenant) == "44"
    assert await _audit_rows(conn, tenant) == []


async def test_non_bypassrls_owner_upgrade_fails_loudly_and_leaves_no_column(rls_db):
    name, owner = rls_db
    c = await asyncpg.connect(f"{_BASE}/{name}")
    try:
        await c.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS home_calling_code")
        await c.execute("TRUNCATE tenants CASCADE")
        tenant = await _tenant(c, destinations=("+918065354620",))
        # FORCE RLS hides the evidence rows from this owner: without the block turning
        # row_security off, every tenant silently got '1' and the apply exited 0.
        with pytest.raises(subprocess.CalledProcessError) as failed:
            _psql(name, "-c", f"SET ROLE {owner}", "-f", str(SCHEMA))
        assert "row-level security" in failed.value.stderr.decode()
        assert await c.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'tenants' AND column_name = 'home_calling_code'"
        ) == 0
        _psql(name, "-f", str(SCHEMA))  # the retry, as a superuser, derives from the data
        assert await _home(c, tenant) == "91"
    finally:
        await c.close()


def test_sql_calling_code_list_equals_the_python_table():
    block = re.search(r"unnest\(ARRAY\[(.*?)\]\)", SCHEMA.read_text(), re.S)
    assert block is not None, "calling-code list not found in schema.sql"
    assert set(re.findall(r"'(\d+)'", block.group(1))) == CALLING_CODES
