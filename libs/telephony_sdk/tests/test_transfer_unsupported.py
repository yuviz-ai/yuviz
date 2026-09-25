from __future__ import annotations

import os

import pytest

os.environ.setdefault("SECRET_ENCRYPTION_KEY", "test-key-not-a-real-fernet-key")

from libs.telephony_sdk.exceptions import TelephonyTransferUnsupported
from libs.telephony_sdk.providers.cloudonix import CloudonixProvider
from libs.telephony_sdk.providers.vobiz import VobizTelephonyProvider


@pytest.mark.asyncio
async def test_vobiz_transfer_call_raises():
    provider = VobizTelephonyProvider({"auth_id": "id", "auth_token": "tok"})
    with pytest.raises(TelephonyTransferUnsupported):
        await provider.transfer_call(call_id="x", destination="y")


@pytest.mark.asyncio
async def test_cloudonix_transfer_call_raises():
    provider = CloudonixProvider({"domain": "example.cloudonix.net", "api_keys": ["enc:x"]})
    with pytest.raises(TelephonyTransferUnsupported):
        await provider.transfer_call(call_id="x", destination="y")
