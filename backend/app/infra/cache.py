from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator

from redis.asyncio import Redis

from ..config import get_settings


class Cache:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.client: Redis | None = Redis.from_url(self.settings.redis_url, decode_responses=True) if self.settings.redis_enabled else None

    async def ping(self) -> bool:
        if not self.client:
            return False
        return bool(await self.client.ping())

    async def close(self) -> None:
        if self.client:
            await self.client.aclose()

    @asynccontextmanager
    async def lease(self, key: str, ttl_seconds: int | None = None) -> AsyncIterator[bool]:
        if not self.client:
            yield True
            return
        token = secrets.token_hex(16)
        ttl = ttl_seconds or self.settings.task_lease_ttl_seconds
        lease_key = f"tilian:{self.settings.app_env}:lease:{key}"
        acquired = bool(await self.client.set(lease_key, token, ex=ttl, nx=True))
        try:
            yield acquired
        finally:
            if acquired:
                script = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
                await self.client.eval(script, 1, lease_key, token)


cache = Cache()
