-- Extend a task lease only if the caller still holds it.
-- KEYS[1] = lease key
-- ARGV[1] = agent_id, ARGV[2] = ttl_ms
-- returns 1 = extended, 0 = no lease present, -1 = held by someone else
local v = redis.call('GET', KEYS[1])
if not v then
  return 0
end
local ok, data = pcall(cjson.decode, v)
if ok and data['agent_id'] ~= ARGV[1] then
  return -1
end
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
return 1
