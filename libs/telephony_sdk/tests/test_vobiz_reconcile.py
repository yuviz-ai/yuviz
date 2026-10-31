"""Vobiz reconcile_call: 'not_placed' only when no queued, live or recorded call matches (issue #217)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from libs.telephony_sdk.providers.vobiz import VobizTelephonyProvider

TO, FROM = "+919876543210", "+918065354620"
SENT = datetime.now(timezone.utc) - timedelta(minutes=2)


def _cdr(uuid: str, *, to: str = TO, frm: str = FROM, at: datetime = SENT + timedelta(seconds=5)) -> dict:
    return {"call_uuid": uuid, "to_number": to.lstrip("+"), "from_number": frm.lstrip("+"),
            "initiation_time": at.astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat(sep=" ")}


def _provider(cdrs: list[dict], *, live: list[str] = (), queued: list[str] = (), details: dict | None = None,
              total: int | None = None, seen: list | None = None) -> VobizTelephonyProvider:
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        status = req.url.params.get("status")
        if req.url.path.endswith("/Call/") and status:
            return httpx.Response(200, json={"calls": list(live if status == "live" else queued)})
        if status:
            call_id = req.url.path.rstrip("/").rsplit("/", 1)[-1]
            return httpx.Response(200, json=(details or {})[call_id])
        offset, limit = int(req.url.params["offset"]), int(req.url.params["limit"])
        return httpx.Response(200, json={"objects": cdrs[offset:offset + limit],
                                         "meta": {"total_count": len(cdrs) if total is None else total}})

    p = VobizTelephonyProvider({"auth_id": "MA_X", "auth_token": "tok"})
    p._http_transport = httpx.MockTransport(handler)
    return p


async def _reconcile(p, **overrides):
    kwargs = {"reference": "k", "observed_call_id": None, "to": TO, "from_number": FROM, "created_after": SENT}
    return await p.reconcile_call(**{**kwargs, **overrides})


async def test_not_placed_when_no_call_matches():
    result = await _reconcile(_provider([_cdr("other", to="+919999999999"), _cdr("old", at=SENT - timedelta(hours=2))]))
    assert result.outcome == "not_placed"


async def test_recorded_call_is_placed_with_its_id():
    result = await _reconcile(_provider([_cdr("c-1")]))
    assert (result.outcome, result.provider_call_id) == ("placed", "c-1")


async def test_live_call_to_the_number_is_placed():
    p = _provider([], live=["c-live"], details={"c-live": {"to": TO.lstrip("+"), "from": FROM.lstrip("+")}})
    result = await _reconcile(p)
    assert (result.outcome, result.provider_call_id) == ("placed", "c-live")


async def test_open_call_without_to_from_is_indeterminate():
    p = _provider([], queued=["q-1"], details={"q-1": {"call_uuid": "q-1"}})
    assert (await _reconcile(p)).outcome == "indeterminate"


async def test_two_matches_are_indeterminate():
    assert (await _reconcile(_provider([_cdr("a"), _cdr("b", at=SENT + timedelta(seconds=1))]))).outcome == "indeterminate"


async def test_list_not_newest_first_is_indeterminate():
    cdrs = [_cdr("old", to="+919999999999", at=SENT - timedelta(hours=1)), _cdr("new", to="+919999999998")]
    assert (await _reconcile(_provider(cdrs))).outcome == "indeterminate"


async def test_history_beyond_the_page_cap_is_indeterminate():
    cdrs = [_cdr(f"x{i}", to="+919999999999", at=SENT + timedelta(seconds=60 - i * 0.1)) for i in range(400)]
    assert (await _reconcile(_provider(cdrs))).outcome == "indeterminate"


async def test_short_page_before_total_is_indeterminate():
    cdrs = [_cdr(f"x{i}", to="+919999999999", at=SENT + timedelta(seconds=60 - i)) for i in range(20)]
    assert (await _reconcile(_provider(cdrs, total=50))).outcome == "indeterminate"


async def test_vobiz_error_is_logged_without_numbers(caplog):
    p = VobizTelephonyProvider({"auth_id": "MA_X", "auth_token": "tok"})
    p._http_transport = httpx.MockTransport(lambda req: httpx.Response(401))
    with caplog.at_level("WARNING"):
        await _reconcile(p)
    assert "reference=k: TelephonyProviderError" in caplog.text and TO not in caplog.text


async def test_send_too_recent_is_indeterminate_without_asking_vobiz():
    seen: list = []
    p = _provider([], seen=seen)
    assert (await _reconcile(p, created_after=datetime.now(timezone.utc))).outcome == "indeterminate"
    assert seen == []


async def test_non_e164_number_is_indeterminate():
    assert (await _reconcile(_provider([]), to="98765 43210")).outcome == "indeterminate"


async def test_vobiz_error_is_indeterminate():
    p = VobizTelephonyProvider({"auth_id": "MA_X", "auth_token": "tok"})
    p._http_transport = httpx.MockTransport(lambda req: httpx.Response(500))
    assert (await _reconcile(p)).outcome == "indeterminate"


async def test_without_hints_falls_back_to_never_guessing():
    p = _provider([])
    assert (await p.reconcile_call(reference="k", observed_call_id=None)).outcome == "indeterminate"


async def test_single_old_record_then_nothing_is_not_placed():
    assert (await _reconcile(_provider([_cdr("old", at=SENT - timedelta(hours=1))]))).outcome == "not_placed"
