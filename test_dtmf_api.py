#!/usr/bin/env python3
import asyncio
import sys
import os

# Add services to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'services/conversation/generated'))
sys.path.insert(0, os.path.dirname(__file__))

import httpx
from services.telephony import orchestrator, app
from services.telephony.accounts import Account
from libs.telephony_sdk.providers.vobiz import VobizTelephonyProvider

async def test_dtmf_endpoint():
    """Test DTMF webhook endpoint"""
    
    base_url = "http://localhost:8750"
    
    print("\n=== DTMF Webhook API Test ===\n")
    
    # Test 1: Missing required fields
    print("Test 1: Missing call_id (should fail gracefully)")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/vobiz/dtmf/test-account",
                headers={
                    "X-Vobiz-Signature-V3": "test-sig",
                    "X-Vobiz-Signature-V3-Nonce": "test-nonce",
                },
                json={"digit": "1"}
            )
            print(f"   Status: {resp.status_code}")
            print(f"   Response: {resp.text}\n")
    except Exception as e:
        print(f"   Error: {e}\n")
    
    # Test 2: Unknown provider
    print("Test 2: Unknown provider (should return 404)")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/unknown-provider/dtmf/test-account",
                json={"call_id": "test-123", "digit": "1"}
            )
            print(f"   Status: {resp.status_code}")
            print(f"   Response: {resp.text}\n")
    except Exception as e:
        print(f"   Error: {e}\n")
    
    # Test 3: Valid provider, invalid signature
    print("Test 3: Invalid signature (should return 403)")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/vobiz/dtmf/test-account",
                headers={
                    "X-Vobiz-Signature-V3": "invalid-signature",
                    "X-Vobiz-Signature-V3-Nonce": "test-nonce",
                },
                json={
                    "call_id": "test-call-123",
                    "digit": "1",
                    "from": "+14155551234",
                    "to": "+14155555678"
                }
            )
            print(f"   Status: {resp.status_code}")
            print(f"   Response: {resp.text}\n")
    except Exception as e:
        print(f"   Error: {e}\n")

if __name__ == "__main__":
    asyncio.run(test_dtmf_endpoint())
