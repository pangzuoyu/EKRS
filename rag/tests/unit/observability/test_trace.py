"""Unit tests for trace contextvar helpers + header extractor.

The middleware (ObservabilityMiddleware) is exercised by the integration test.
"""
import asyncio

from ekrs_rag.observability.trace import (
    get_trace_id, set_trace_id, reset_trace_id,
    get_skip_audit, set_skip_audit, reset_skip_audit,
)


def test_default_trace_id_is_unknown():
    # Outside any context, returns "unknown"
    assert get_trace_id() == "unknown"


def test_set_and_reset_trace_id():
    token = set_trace_id("test-trace-123")
    try:
        assert get_trace_id() == "test-trace-123"
    finally:
        reset_trace_id(token)
    assert get_trace_id() == "unknown"


def test_skip_audit_default_false():
    assert get_skip_audit() is False


def test_skip_audit_set_and_reset():
    token = set_skip_audit(True)
    try:
        assert get_skip_audit() is True
    finally:
        reset_skip_audit(token)
    assert get_skip_audit() is False


def test_skip_audit_isolated_across_async_tasks():
    """Two concurrent tasks must not see each other's skip_audit flag."""
    async def task(skip, barrier):
        set_skip_audit(skip)
        await barrier.wait()
        seen = get_skip_audit()
        return seen

    async def main():
        barrier = asyncio.Barrier(2)
        results = await asyncio.gather(
            task(True, barrier),
            task(False, barrier),
        )
        assert True in results
        assert False in results

    asyncio.run(main())


def test_trace_id_isolated_across_async_tasks():
    """Two concurrent tasks must not see each other's trace_id."""
    async def task(tid, barrier):
        set_trace_id(tid)
        await barrier.wait()
        seen = get_trace_id()
        return seen

    async def main():
        barrier = asyncio.Barrier(2)
        results = await asyncio.gather(
            task("task-A", barrier),
            task("task-B", barrier),
        )
        # Each task sees its own trace_id after await
        assert "task-A" in results
        assert "task-B" in results
        assert results[0] != results[1]

    asyncio.run(main())


def test_trace_id_from_header_or_generated():
    """Middleware behavior: use X-Trace-Id header, else generate uuid4."""
    from ekrs_rag.api.middleware.observability import (
        extract_or_generate_trace_id,
    )
    # No header → generated uuid4
    generated = extract_or_generate_trace_id(headers={})
    assert len(generated) == 36  # uuid4 hex with dashes
    # With header → use as-is
    provided = extract_or_generate_trace_id(headers={"x-trace-id": "my-custom"})
    assert provided == "my-custom"