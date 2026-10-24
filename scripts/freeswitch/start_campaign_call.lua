-- start_campaign_call.lua — dialplan script for the trigger INVITE Kamailio sends for a campaign dial.
--
-- The intent in Redis (written only by Campaigns, consumed once here) is the sole source of the callee,
-- caller ID, tenant, agent and time limit; the INVITE supplies only the key. Any doubt answers "rejected" and
-- dials nothing. The reply is a 603 whose X-Campaign-Result Kamailio logs: accepted, duplicate or rejected.

local EGRESS_GRACE_S = 120   -- Campaigns' egress token outlives the intent by this much (dial_request.py)

local api = freeswitch.API()

local function reply(result)
    session:setVariable("sip_rh_X-Campaign-Result", result)
    session:execute("respond", "603 Decline")
end

local function reject(why)
    freeswitch.consoleLog("WARNING", "start_campaign_call: rejected: " .. why .. "\n")
    return reply("rejected")
end

-- hiredis_raw answers "-ERR no reply" for a nil reply and "-ERR ..." for an error, so a real value never starts with "-".
local function field(raw, name, pattern)
    return raw:match('"' .. name .. '"%s*:%s*"(' .. pattern .. ')"')
end

local key = session:getVariable("sip_h_X-Idempotency-Key") or ""
local key_id, key_attempt = key:match("^([0-9a-f%-]+)_(%d+)$")
if not key_id or #key_id ~= 36 or #key_attempt > 3 then return reject("malformed key") end

local raw = api:executeString("hiredis_raw default getdel campaign:intent:" .. key)
if raw:sub(1, 1) ~= "{" or raw:sub(-1) ~= "}" then return reject("no intent") end

local intent = {
    key = field(raw, "key", "[0-9a-f%-]+_%d+"),
    call_uuid = field(raw, "call_uuid", "[0-9a-f%-]+"),
    tenant_id = field(raw, "tenant_id", "[0-9a-f%-]+"),
    callee = field(raw, "callee", "%+[1-9]%d+"),
    caller_id = field(raw, "caller_id", "%+[1-9]%d+"),
    tenant_slug = field(raw, "tenant_slug", "[a-z0-9][a-z0-9_%-]*"),
    agent_slug = field(raw, "agent_slug", "[a-z0-9][a-z0-9_%-]*"),
    egress_token = field(raw, "egress_token", "[%w%._%+%-]+"),
    exp = tonumber(raw:match('"exp"%s*:%s*(%d+)')),
    max_call_s = raw:match('"max_call_s"%s*:%s*(%d+)'),
}
for _, name in ipairs({"key", "call_uuid", "tenant_id", "callee", "caller_id", "tenant_slug", "agent_slug", "egress_token", "exp", "max_call_s"}) do
    if not intent[name] then return reject("intent lacks " .. name) end
end
if intent.key ~= key then return reject("intent is for another key") end
if intent.exp < os.time() then return reject("intent expired") end
if not intent.max_call_s:match("^[1-9]%d%d?%d?$") then return reject("bad max_call_s") end

-- The token is minted from the same values; one that disagrees means the intent was tampered with.
local t_key, t_exp, t_tenant, t_caller, t_max =
    intent.egress_token:match("^([^.]+)%.(%d+)%.([^.]+)%.([^.]+)%.(%d+)%.[%w_%-]+$")
if t_key ~= intent.key or tonumber(t_exp) ~= intent.exp + EGRESS_GRACE_S or t_tenant ~= intent.tenant_id
        or t_caller ~= intent.caller_id or t_max ~= intent.max_call_s then
    return reject("egress token disagrees with the intent")
end

local claimed = api:executeString("hiredis_raw default set campaign:claim:" .. key .. " " .. intent.call_uuid .. "|"
    .. (session:getVariable("local_ip_v4") or "") .. " NX EX 105")
-- A nil SET NX reply (key already claimed) reaches Lua as "" or "-ERR no reply"; either way nothing is dialled.
if claimed == "" or claimed == "-ERR no reply" then return reply("duplicate") end
if claimed ~= "OK" then return reject("claim failed: " .. claimed) end

local vars = table.concat({
    "origination_uuid=" .. intent.call_uuid,
    "origination_caller_id_number=" .. intent.caller_id,
    "originate_timeout=45",
    "campaign_key=" .. key,
    "campaign_caller_id=" .. intent.caller_id,
    "campaign_tenant_id=" .. intent.tenant_id,
    "campaign_tenant_slug=" .. intent.tenant_slug,
    "campaign_agent_slug=" .. intent.agent_slug,
    "campaign_callee=" .. intent.callee,
    "sip_h_X-Egress-Token=" .. intent.egress_token,
    "sip_cid_type=pid",
    "execute_on_answer='sched_hangup +" .. intent.max_call_s .. " ALLOTTED_TIMEOUT'",
}, ",")
api:executeString("bgapi originate {" .. vars .. "}sofia/gateway/kamailio_egress/" .. intent.callee .. " &lua(start_voice_ai.lua)")
freeswitch.consoleLog("NOTICE", "start_campaign_call: dialling key=" .. key .. " uuid=" .. intent.call_uuid .. "\n")
reply("accepted")
