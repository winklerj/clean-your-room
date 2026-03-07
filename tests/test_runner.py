import pytest
import asyncio
from unittest.mock import AsyncMock
from pathlib import Path

from clean_room.runner import JobRunner
from clean_room.streaming import LogBuffer


@pytest.fixture
def log_buffer():
    return LogBuffer()


@pytest.mark.asyncio
async def test_runner_respects_cancellation(log_buffer):
    """Runner stops iterating when cancel event is set."""
    cancel_event = asyncio.Event()
    cancel_event.set()  # Pre-cancelled

    runner = JobRunner(
        job_id=1,
        repo_path=Path("/tmp/fake"),
        specs_path=Path("/tmp/fake-specs"),
        prompt="test prompt",
        max_iterations=10,
        log_buffer=log_buffer,
        cancel_event=cancel_event,
    )

    with pytest.MonkeyPatch.context() as m:
        mock_agent = AsyncMock()
        m.setattr(runner, "_run_agent_iteration", mock_agent)
        await runner.run(db=AsyncMock())
        mock_agent.assert_not_called()


@pytest.mark.asyncio
async def test_runner_iterates_up_to_max(log_buffer):
    """Runner calls agent for each iteration up to max."""
    runner = JobRunner(
        job_id=1,
        repo_path=Path("/tmp/fake"),
        specs_path=Path("/tmp/fake-specs"),
        prompt="test prompt",
        max_iterations=3,
        log_buffer=log_buffer,
        cancel_event=asyncio.Event(),
    )

    with pytest.MonkeyPatch.context() as m:
        mock_agent = AsyncMock(return_value="iteration output")
        m.setattr(runner, "_run_agent_iteration", mock_agent)
        await runner.run(db=AsyncMock())
        assert mock_agent.call_count == 3


@pytest.mark.asyncio
async def test_runner_logs_each_iteration(log_buffer):
    """Runner appends to log buffer for each iteration."""
    runner = JobRunner(
        job_id=1,
        repo_path=Path("/tmp/fake"),
        specs_path=Path("/tmp/fake-specs"),
        prompt="test prompt",
        max_iterations=2,
        log_buffer=log_buffer,
        cancel_event=asyncio.Event(),
    )

    with pytest.MonkeyPatch.context() as m:
        m.setattr(runner, "_run_agent_iteration", AsyncMock(return_value="output"))
        await runner.run(db=AsyncMock())
    history = log_buffer.get_history(1)
    assert len(history) >= 2


@pytest.mark.asyncio
async def test_runner_closes_buffer_on_completion(log_buffer):
    """Runner closes the log buffer when done."""
    runner = JobRunner(
        job_id=1,
        repo_path=Path("/tmp/fake"),
        specs_path=Path("/tmp/fake-specs"),
        prompt="test prompt",
        max_iterations=1,
        log_buffer=log_buffer,
        cancel_event=asyncio.Event(),
    )

    with pytest.MonkeyPatch.context() as m:
        m.setattr(runner, "_run_agent_iteration", AsyncMock(return_value="done"))
        await runner.run(db=AsyncMock())
    assert 1 in log_buffer._closed
