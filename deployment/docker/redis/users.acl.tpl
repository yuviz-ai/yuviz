# Rendered by scripts/render_redis_acl.sh: each user's password-hash placeholder becomes #<sha256>.
user default off resetpass resetkeys resetchannels -@all
user gateway on #{{gateway}} resetkeys resetchannels -@all +auth +hello +ping +get +mget ~did:* ~routing:ready ~tenant:*
user config on #{{config}} resetkeys resetchannels -@all +auth +hello +ping +client|setinfo +del +exists +get +publish +set ~agent:* ~callflow:* ~did:* ~provider:* ~routing:ready ~telephony:health:* ~telephony_config:* ~tenant:* ~testcred:* &provider_config_changed
user did on #{{did}} resetkeys resetchannels -@all +auth +hello +ping +client|setinfo
user knowledge on #{{knowledge}} resetkeys resetchannels -@all +auth +hello +ping +client|setinfo +del +get +scan +set ~agent_kb:* ~kbflag2:* ~plan:* ~qemb:*
user conversation on #{{conversation}} resetkeys resetchannels -@all +auth +hello +ping +client|setinfo +del +get +set +subscribe +unsubscribe ~agent:* ~callflow:* ~kbflag2:* ~provider:* ~tenant:* ~testcred:* &provider_config_changed (~campaign:events:conv +xadd)
user telephony on #{{telephony}} resetkeys resetchannels -@all +auth +hello +ping +client|setinfo (~agent:* ~did:* +get) (~idem:* ~idemref:* +get +set +del) (~telephony:health:* +set) (~telephony:webhook_nonce:* +set) (~campaign:events:cpaas +xadd)
user campaigns on #{{campaigns}} resetkeys resetchannels ~campaign:claim:* ~campaign:intent:* ~campaign:events:* -@all +auth +hello +ping +client|setinfo +get +set +del +exists +getdel +xreadgroup +xack +xautoclaim +xgroup|create +xadd
user freeswitch on #{{freeswitch}} resetkeys resetchannels -@all +auth +hello +ping (~campaign:intent:* +getdel) (~campaign:claim:* +set) (~campaign:events:fs +xadd)
