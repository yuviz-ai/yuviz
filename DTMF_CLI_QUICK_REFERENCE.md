# DTMF CLI Quick Reference

## Fast Commands to Test from Terminal

### 1️⃣ Test Unknown Provider (Should return 404)
```bash
curl -X POST http://localhost:8750/badprovider/dtmf/test-account \
  -H "Content-Type: application/json" \
  -d '{"call_id":"test","digit":"1"}'
```

### 2️⃣ Test Vobiz Provider (Invalid Signature)
```bash
curl -X POST http://localhost:8750/vobiz/dtmf/test-account \
  -H "X-Vobiz-Signature-V3: invalid-sig" \
  -H "X-Vobiz-Signature-V3-Nonce: test-nonce" \
  -H "Content-Type: application/json" \
  -d '{"call_id":"call-123","digit":"1","from":"+14155551234","to":"+14155555678"}'
```

**Expected Response:** `404 {"detail":"Not Found"}`

### 3️⃣ Test Cloudonix Provider (Invalid Key)
```bash
curl -X POST http://localhost:8750/cloudonix/dtmf/test-account \
  -H "X-CX-APIKey: invalid-key" \
  -H "Content-Type: application/json" \
  -d '{"callsid":"call-456","digit":"2","from":"+14155551234","to":"+14155555678"}'
```

**Expected Response:** `404 {"detail":"Not Found"}`

### 4️⃣ Test with Verbose Output (See Headers & Full Response)
```bash
curl -v -X POST http://localhost:8750/vobiz/dtmf/test-account \
  -H "X-Vobiz-Signature-V3: sig" \
  -H "X-Vobiz-Signature-V3-Nonce: nonce" \
  -H "Content-Type: application/json" \
  -d '{"call_id":"test","digit":"1"}' 2>&1 | grep -E "< HTTP|< |{"
```

### 5️⃣ Test with Real Account (Once You Have One)
```bash
curl -X POST http://localhost:8750/vobiz/dtmf/YOUR_ACCOUNT_REF \
  -H "X-Vobiz-Signature-V3: YOUR_SIGNATURE" \
  -H "X-Vobiz-Signature-V3-Nonce: YOUR_NONCE" \
  -H "Content-Type: application/json" \
  -d '{
    "call_id": "ACTIVE_CALL_UUID",
    "digit": "1",
    "from": "+14155551234",
    "to": "+14155555678"
  }'
```

---

## Understanding Responses

### ✅ 200 OK
```json
ok
```
- DTMF processed successfully
- Check Conversation Service logs for routing

### ❌ 404 Not Found
```json
{"detail": "Not Found"}
```
- Unknown provider OR
- Account doesn't exist OR
- Call session not found in mapping

### ❌ 403 Forbidden
```json
{"detail": "Forbidden"}
```
- Invalid signature OR
- Invalid API key

### ❌ 429 Too Many Requests
```json
{"detail": "Too Many Requests"}
```
- Rate limit exceeded (300 req/min per account)

### ❌ 500 Internal Server Error
```json
{"detail": "Internal Server Error"}
```
- gRPC transmission failed OR
- Unexpected server error

---

## Complete Testing Workflow

### Step 1: Verify Service is Running
```bash
curl http://localhost:8750/health
# Expected: {"loaded":true}
```

### Step 2: Run Automated Tests
```bash
bash /Users/satish/voice-ai-platform/dtmf_cli_test.sh
```

### Step 3: Test with Real Account
1. Find your account:
   ```bash
   # Query your database
   SELECT provider, account_ref FROM telephony_configs LIMIT 5;
   ```

2. Make a test request:
   ```bash
   curl -X POST http://localhost:8750/{provider}/dtmf/{account_ref} \
     -H "X-{Provider}-Signature: signature" \
     -H "Content-Type: application/json" \
     -d '{"call_id":"test","digit":"1"}'
   ```

3. Check logs:
   ```bash
   tail -f /tmp/telephony.log | grep DTMF
   ```

---

## Useful Alias for Quick Testing

Add to your `~/.bashrc` or `~/.zshrc`:

```bash
# DTMF quick test
alias dtmf-test='curl -X POST http://localhost:8750/vobiz/dtmf/test \
  -H "X-Vobiz-Signature-V3: sig" \
  -H "X-Vobiz-Signature-V3-Nonce: nonce" \
  -H "Content-Type: application/json" \
  -d "{\"call_id\":\"test-$(date +%s)\",\"digit\":\"1\"}"'

# Then use:
# dtmf-test
```

---

## Debugging Tips

### See Full Request/Response
```bash
curl -v http://localhost:8750/vobiz/dtmf/test \
  --data '...' 2>&1 | tee /tmp/dtmf_debug.txt
```

### Test Multiple Digits Sequentially
```bash
for digit in 1 2 3; do
  echo "Testing digit $digit..."
  curl -s http://localhost:8750/vobiz/dtmf/test \
    -d "{\"digit\":\"$digit\"}" | head -1
done
```

### Monitor Service Logs in Real-time
```bash
tail -f /tmp/telephony.log | grep -E "DTMF|dtmf|error"
```

### Check if Route is Registered
```bash
curl -s -X OPTIONS http://localhost:8750/ -v 2>&1 | grep dtmf
```

---

## Expected Behavior Summary

| Test | Expected Status | Reason |
|------|-----------------|--------|
| Unknown provider | 404 | Provider not recognized |
| Valid provider, invalid account | 404 | Account doesn't exist |
| Valid provider, invalid signature | 403 | Signature verification failed |
| Valid account, no active call | 200 + silent drop | Call not found in mapping |
| Valid call, valid DTMF | 200 | Processed, routed to agent |

---

## Next: Full End-to-End Test

Once you have:
1. ✅ Active call (from Vobiz/Cloudonix)
2. ✅ Valid telephony_config in database
3. ✅ CallFlow configured with DTMF routes

Then:
```bash
# Send DTMF during active call
curl -X POST http://localhost:8750/vobiz/dtmf/YOUR_ACCOUNT \
  -H "X-Vobiz-Signature-V3: $(calculate_signature)" \
  -H "X-Vobiz-Signature-V3-Nonce: $(generate_nonce)" \
  -H "Content-Type: application/json" \
  -d '{"call_id":"ACTIVE_CALL_ID","digit":"1"}'

# Expected: Agent receives DTMF, routes to Sales, greeting plays ✓
```

---

## Resources

- **DTMF Architecture Guide**: `/artifacts/67951b3e-f693-434c-b2f4-977779b75e67`
- **API Reference**: `/artifacts/f63c1d0c-91e4-4555-b290-ddd3fce0d208`
- **Test Script**: `bash /Users/satish/voice-ai-platform/dtmf_cli_test.sh`
- **Setup Guide**: `bash /Users/satish/voice-ai-platform/setup_and_test_dtmf.sh`
