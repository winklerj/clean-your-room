import pytest
import asyncio

from clean_room.streaming import LogBuffer


@pytest.mark.asyncio
async def test_append_and_read():
    """Appending a message makes it available to readers."""
    buf = LogBuffer()
    buf.append(1, "hello")
    messages = buf.get_history(1)
    assert messages == ["hello"]


@pytest.mark.asyncio
async def test_subscribe_receives_new_messages():
    """Subscriber receives messages appended after subscription."""
    buf = LogBuffer()
    received = []

    async def reader():
        async for msg in buf.subscribe(1):
            received.append(msg)
            if len(received) >= 2:
                break

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.01)
    buf.append(1, "first")
    buf.append(1, "second")
    await asyncio.wait_for(task, timeout=1.0)
    assert received == ["first", "second"]


@pytest.mark.asyncio
async def test_close_terminates_subscribers():
    """Closing a job's buffer terminates all active subscribers."""
    buf = LogBuffer()
    received = []

    async def reader():
        async for msg in buf.subscribe(1):
            received.append(msg)

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.01)
    buf.append(1, "msg")
    buf.close(1)
    await asyncio.wait_for(task, timeout=1.0)
    assert received == ["msg"]


@pytest.mark.asyncio
async def test_multiple_jobs_isolated():
    """Messages for different jobs don't leak across subscribers."""
    buf = LogBuffer()
    buf.append(1, "job1")
    buf.append(2, "job2")
    assert buf.get_history(1) == ["job1"]
    assert buf.get_history(2) == ["job2"]
