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

local gateway_ws = os.getenv("VOICE_AI_GATEWAY_WS") or ("ws://127.0.0.1:" .. (os.getenv("GATEWAY_LISTEN_PORT") or "8080"))

local uuid = session:get_uuid()
session:answer()

-- The inbound values come from the caller's own SIP INVITE. Anything outside this
-- allowlist could break out of the JSON (e.g. inject a different "did") or
-- split the API args.
local function sip_token(value)
    return (tostring(value or ""):gsub("[^%w%+%-%._@]", ""))
end

-- The originating FreeSWITCH node, used by the Gateway to route ESL commands
-- (transfer, hangup) to the correct node in a multi-node deployment.
local fs_host = session:getVariable("local_ip_v4") or ""

-- A campaign call (campaign_key comes from the consumed dial intent, never a header) is outbound: its
-- agent is the campaign's own (tenant, agent) slugs, never inferred from the caller ID, which is only the number
-- shown to the callee and goes in `did` for records; the remote party, the callee, is the ani.
-- A clinic reminder sets yuviz_remote_ani server-side (an INVITE cannot), so the agent sees the patient
-- it dialled rather than the hospital's own caller ID.
local did, ani, direction, agent_fields
if session:getVariable("campaign_key") then
    did, ani, direction = session:getVariable("campaign_caller_id"), session:getVariable("campaign_callee"), "outbound"
    agent_fields = string.format(',"tenant_slug":"%s","agent_slug":"%s"',
        sip_token(session:getVariable("campaign_tenant_slug")), sip_token(session:getVariable("campaign_agent_slug")))
else
    did = session:getVariable("destination_number")
    ani = session:getVariable("yuviz_remote_ani") or session:getVariable("caller_id_number")
    direction = "inbound"
    agent_fields = ""
end

-- For outbound calls with agent_fields, add a timestamp to prevent replays.
-- The Gateway validates that outbound metadata was generated recently.
local ts_field = ""
if direction == "outbound" then
    ts_field = string.format(',"ts":%d', os.time())
end

local meta = string.format('{"did":"%s","ani":"%s","direction":"%s","freeswitch_host":"%s"%s%s}',
    sip_token(did), sip_token(ani), direction, sip_token(fs_host), agent_fields, ts_field)

local result = freeswitch.API():execute("uuid_audio_fork",
    uuid .. " start " .. gateway_ws .. "/voice/" .. uuid .. " mono 16000 " .. meta)
freeswitch.consoleLog("INFO", "start_voice_ai: uuid_audio_fork " .. uuid .. " -> " .. result .. "\n")

-- Endless silence rather than session:sleep(): sleep writes no frames, and
-- the agent's speech is injected by replacing outbound frames (the
-- write-replace bug in mod_audio_fork-playback.patch), so with nothing
-- being written the caller hears nothing.
session:execute("playback", "silence_stream://-1")
