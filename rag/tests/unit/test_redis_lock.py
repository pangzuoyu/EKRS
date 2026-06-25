import pytest
import fakeredis.aioredis

from ekrs_rag.concurrency.redis_lock import RedisLock


@pytest.fixture
def lock():
    client = fakeredis.aioredis.FakeRedis()
    return RedisLock(client)


@pytest.mark.asyncio
async def test_acquire_returns_token(lock):
    token = await lock.acquire("k1", ttl_sec=10)
    assert token is not None
    assert len(token) == 32  # uuid4 hex


@pytest.mark.asyncio
async def test_acquire_same_key_blocked(lock):
    t1 = await lock.acquire("k1", ttl_sec=10)
    t2 = await lock.acquire("k1", ttl_sec=10)
    assert t1 is not None
    assert t2 is None


@pytest.mark.asyncio
async def test_release_with_correct_token_succeeds(lock):
    t = await lock.acquire("k1", ttl_sec=10)
    assert await lock.release("k1", t) is True
    # 锁释放后能再拿
    t2 = await lock.acquire("k1", ttl_sec=10)
    assert t2 is not None


@pytest.mark.asyncio
async def test_release_with_wrong_token_fails(lock):
    await lock.acquire("k1", ttl_sec=10)
    assert await lock.release("k1", "wrong-token") is False


@pytest.mark.asyncio
async def test_different_keys_independent(lock):
    t1 = await lock.acquire("k1", ttl_sec=10)
    t2 = await lock.acquire("k2", ttl_sec=10)
    assert t1 and t2
