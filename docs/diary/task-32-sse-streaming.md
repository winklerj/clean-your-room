# Task 32: SSE Streaming Endpoints

## What I did
- Added `routes/streams.py` with two SSE endpoints using `sse-starlette` EventSourceResponse
- `GET /pipelines/{id}/stream` — pipeline-scoped log stream that replays history then streams live
- `GET /sessions/{id}/stream` — session-scoped stream that looks up the parent pipeline and subscribes to its LogBuffer
- Updated `pipeline_detail.html` to use EventSource for live log delivery, with HTMX polling as fallback on SSE failure
- Registered streams router in `main.py`
- Updated existing test that checked for HTMX polling attributes to reflect SSE-first architecture

## Learnings
- HTTPX's ASGITransport buffers the entire SSE response rather than delivering chunks incrementally, so "live streaming" tests need a different pattern: pre-stage a delayed append+close and let the response complete naturally rather than trying to read individual SSE events mid-stream
- The LogBuffer singleton persists across tests since it's module-level in `main.py`. Tests that use it need an `autouse` fixture to clear `_history`, `_subscribers`, and `_closed` between runs, otherwise closed channels from earlier tests prevent subscriptions in later ones
- The session stream endpoint subscribes to the parent pipeline's LogBuffer channel. All messages are pipeline-scoped (the stage runners prefix messages with `[stage_type]`), so session-level filtering is left to the client. This avoids a LogBuffer refactor while still providing the SSE endpoint the spec requires.

## Postcondition verification
- [PASS] 17 new tests all green
- [PASS] lint clean (ruff)
- [PASS] type check clean (mypy)
- [PASS] 967/969 total tests pass (2 deselected = pre-existing flaky Hypothesis tests)
