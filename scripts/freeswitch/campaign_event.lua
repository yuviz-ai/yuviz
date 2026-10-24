-- campaign_event.lua — mod_lua event hook (CHANNEL_ANSWER, CHANNEL_HANGUP_COMPLETE): reports the outcome of a
-- campaign callee leg to Campaigns by appending to campaign:events:fs. Channels without campaign_key are ignored.
--
-- The hangup event is used rather than a hangup hook because billmsec is only set by then, and the tenant
-- comes from campaign_tenant_id, which start_campaign_call.lua copied from the consumed intent.

-- Entry fields, as libs/telephony_sdk/campaign_events.encode() writes them (FreeSWITCH has no provider_call_id).
local FIELDS = {"source", "kind", "key", "tenant_id", "call_session_id", "cause", "answered", "billable_ms"}

local function clean(value)
    return (tostring(value or ""):gsub("[^%w_%-%.:]", ""))
end

local key = event:getHeader("variable_campaign_key")
if not key then return end

local hangup = event:getHeader("Event-Name") == "CHANNEL_HANGUP_COMPLETE"
local answered = not hangup or (tonumber(event:getHeader("variable_answer_epoch")) or 0) > 0
local values = {
    source = "fs",
    kind = hangup and "hangup" or "answered",
    key = key,
    tenant_id = event:getHeader("variable_campaign_tenant_id"),
    call_session_id = event:getHeader("Unique-ID"),
    cause = hangup and event:getHeader("Hangup-Cause") or "",
    answered = answered and "1" or "0",
    billable_ms = hangup and tonumber(event:getHeader("variable_billmsec")) or nil,
}

local args = {}
for _, name in ipairs(FIELDS) do
    if values[name] ~= nil and values[name] ~= "" then args[#args + 1] = name .. " " .. clean(values[name]) end
end
local out = freeswitch.API():executeString("hiredis_raw default xadd campaign:events:fs MAXLEN ~ 100000 * " .. table.concat(args, " "))
freeswitch.consoleLog("NOTICE", "campaign_event: " .. values.kind .. " key=" .. key .. " -> " .. out .. "\n")
