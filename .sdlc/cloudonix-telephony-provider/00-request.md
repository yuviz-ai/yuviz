Integrate Cloudonix as a managed telephony provider to replace our self-hosted
Kamailio + FreeSWITCH + custom C++ gateway stack.

Context gathered from developers.cloudonix.com before writing this request:

- Cloudonix is a carrier-agnostic SIP trunk/platform layer, not a DID
  reseller. We keep our existing carrier relationship (Plivo/Twilio via
  services/did/'s IDidProvider registry) for DID provisioning/porting;
  that carrier's SIP trunk gets repointed at a Cloudonix inbound trunk
  instead of our own Kamailio.
- Cloudonix supports a "Remote web-service endpoint" (webhook) model for
  Voice Applications: on an inbound call it sends an HTTP GET/POST to a
  URL we host, with To (the DNID/DID), From (caller ANI), CallSid/Session,
  Domain, and an X-CX-APIKey header for authentication. We respond with
  CXML.
- The CXML response uses <Connect><Stream url="wss://..."> to bridge the
  call's audio to a WebSocket we run, bidirectionally, in Twilio Media
  Streams-compatible protocol: mulaw/8kHz/mono audio, base64-encoded JSON
  messages (connected/start/media/dtmf/stop).
- Once <Connect><Stream> is active, Cloudonix does no TTS/DTMF/interruption
  handling itself — it's a raw bidirectional audio pipe. All barge-in
  (Silero VAD, onset/hold windows) and conversational logic stays entirely
  in our own conversation service, unchanged.
- Outbound trunk routing (to "a remote carrier or vendor platform", with
  prefix/priority config) is documented and should cover cold/blind
  transfer. Whether an already-connected, in-progress <Connect><Stream>
  call supports a true warm handoff mid-call is NOT confirmed in the docs
  and needs to be verified against a real trial account.
- We now have a Cloudonix API key to test against a real account.

What we want built:
1. A small, publicly-reachable HTTP webhook service that receives
   Cloudonix's per-call request, resolves the DID via our EXISTING
   did:{did} Redis lookup (unchanged from today's gateway), and returns
   CXML with <Connect><Stream> pointing at our own WebSocket endpoint.
2. A WebSocket bridge service — architecturally close to the existing
   services/webcall/__main__.py pattern (JSON-over-WebSocket -> gRPC
   Converse()) — that speaks Cloudonix's Twilio-compatible protocol
   instead of webcall's raw PCM16/16kHz frames: mulaw<->PCM16 transcoding
   and 8kHz<->16kHz resampling, translating start/media/dtmf/stop messages
   into our existing gRPC session lifecycle.
3. Verification, against a real Cloudonix trial account with the provided
   API key, of: (a) a full round-trip test call through the webhook +
   stream bridge into our existing conversation engine, and (b) whether
   warm mid-call transfer to an external number is actually supported.

Explicitly out of scope for this feature: replacing our DID
provisioning/porting (stays on the existing carrier), and any changes to
the conversation engine itself (STT/LLM/TTS/orchestrator) — only the
telephony/transport leg changes.
