-- start_voice_ai.lua — FreeSWITCH dialplan script for AI agent calls.
--
-- Three jobs: answer, fork the caller's audio to the Gateway, park.
-- Everything after that (hangup, warm transfer) is driven by the Gateway
-- over ESL, so this script only has to keep the channel alive.
--
-- Load-bearing details (see gateway/src/core/Application.cpp and
-- gateway/src/config/Config.cpp):
--   - The channel UUID in the URL path becomes session_id, the calls-table PK.
--   - Metadata keys are exactly did/ani/direction; malformed or missing metadata
--     gets the call rejected as "unavailable" (never routed to a default agent).
--   - mod_audio_fork splits its API args on spaces, so the metadata JSON
--     must not contain any.

local gateway_ws = os.getenv("VOICE_AI_GATEWAY_WS") or "ws://127.0.0.1:8080"

local uuid = session:get_uuid()
session:answer()

-- Both values come from the caller's own SIP INVITE. Anything outside this
-- allowlist could break out of the JSON (e.g. inject a different "did") or
-- split the API args.
local function sip_token(value)
    return (tostring(value or ""):gsub("[^%w%+%-%._@]", ""))
end

-- The originating FreeSWITCH node, used by the Gateway to route ESL commands
-- (transfer, hangup) to the correct node in a multi-node deployment.
local fs_host = session:getVariable("local_ip_v4") or ""

local meta = string.format('{"did":"%s","ani":"%s","direction":"inbound","freeswitch_host":"%s"}',
    sip_token(session:getVariable("destination_number")),
    sip_token(session:getVariable("caller_id_number")),
    sip_token(fs_host))

local result = freeswitch.API():execute("uuid_audio_fork",
    uuid .. " start " .. gateway_ws .. "/voice/" .. uuid .. " mono 16000 " .. meta)
freeswitch.consoleLog("INFO", "start_voice_ai: uuid_audio_fork " .. uuid .. " -> " .. result .. "\n")

-- Endless silence rather than session:sleep(): sleep writes no frames, and
-- the agent's speech is injected by replacing outbound frames (the
-- write-replace bug in mod_audio_fork-playback.patch), so with nothing
-- being written the caller hears nothing.
session:execute("playback", "silence_stream://-1")
