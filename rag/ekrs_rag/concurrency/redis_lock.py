"""Redis 分布式锁: SET NX EX + Lua 释放 token 校验."""
from __future__ import annotations

import uuid

# Lua: 仅当 token 匹配才删除 (避免持锁者过期后误删新持有者的锁)
_RELEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
else
  return 0
end
"""


class RedisLock:
    def __init__(self, redis_client):
        self._r = redis_client
        self._release_script_sha: str | None = None

    async def _ensure_release_script(self) -> str:
        if self._release_script_sha is None:
            self._release_script_sha = await self._r.script_load(_RELEASE_LUA)
        return self._release_script_sha

    async def acquire(self, key: str, ttl_sec: int) -> str | None:
        token = uuid.uuid4().hex
        ok = await self._r.set(key, token, nx=True, ex=ttl_sec)
        return token if ok else None

    async def release(self, key: str, token: str) -> bool:
        sha = await self._ensure_release_script()
        result = await self._r.evalsha(sha, 1, key, token)
        return bool(result)
