import assert from "node:assert/strict";
import test from "node:test";
import { callbackFailureMessage, DECLINED_MESSAGE, FAILURE_MESSAGE } from "../../lib/oauthCallback.ts";

test("access_denied says the connection was declined", () => {
  assert.equal(callbackFailureMessage("access_denied"), DECLINED_MESSAGE);
  assert.match(DECLINED_MESSAGE, /declined/);
});

test("other or missing provider errors get the generic message and never echo provider text", () => {
  for (const e of ["server_error", "invalid_scope", "", null, "ACCESS_DENIED", "access_denied <script>"]) {
    assert.equal(callbackFailureMessage(e), FAILURE_MESSAGE);
  }
  assert.equal(callbackFailureMessage("temporarily_unavailable").includes("temporarily"), false);
});
