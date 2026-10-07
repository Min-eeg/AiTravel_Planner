"""可插拔缓存抽象。

设计目标：当前用内存缓存，未来接 Redis 时只需新增一个 BaseCache 子类
并替换全局 `cache` 实例，业务代码零改动。

分工原则：数据库存"用户要留下来的行程"，缓存存"短期内可复用的外部查询结果"。
"""

import hashlib
import json
import time
from typing import Any, Dict, Optional, Tuple


class BaseCache:
    """缓存接口。"""

    def get(self, key: str) -> Optional[Any]:
        raise NotImplementedError

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def clear(self) -> None:
        raise NotImplementedError


class MemoryCache(BaseCache):
    """单进程内存缓存：{key: (value, expire_at)}。"""

    def __init__(self, default_ttl: int = 3600) -> None:
        self.default_ttl = default_ttl
        self._store: Dict[str, Tuple[Any, float]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[Any]:
        item = self._store.get(key)
        if item is None:
            self.misses += 1
            return None
        value, expire_at = item
        if time.time() >= expire_at:
            del self._store[key]
            self.misses += 1
            return None
        self.hits += 1
        return value

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        effective = self.default_ttl if ttl is None else ttl
        self._store[key] = (value, time.time() + effective)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 3) if total else 0.0


# Redis 实现骨架：接入时取消注释 + 实现即可，业务代码无需改动
#
# class RedisCache(BaseCache):
#     def __init__(self, url: str = "redis://localhost:6379/0"):
#         import redis.asyncio as aioredis
#         self._r = aioredis.from_url(url, decode_responses=True)
#
#     async def get(self, key: str):
#         raw = await self._r.get(key)
#         return json.loads(raw) if raw else None
#
#     async def set(self, key: str, value: Any, ttl: Optional[int] = None):
#         await self._r.set(key, json.dumps(value, ensure_ascii=False),
#                          ex=ttl or 3600)


# 全局缓存实例（测试可注入 FakeCache 替换）
cache: BaseCache = MemoryCache()


def make_cache_key(prefix: str, **params: Any) -> str:
    """稳定的缓存键：参数排序序列化后取 md5 前 12 位。"""
    raw = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}:{digest}"


def reset_cache() -> None:
    """仅供测试使用。"""
    cache.clear()