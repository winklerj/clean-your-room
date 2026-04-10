# Task 11: CodexAppServerAdapter

## What was built

CodexAppServerAdapter — wraps the Codex app-server stdio JSON-RPC protocol for
automated code review and implementation stages. The adapter spawns a `codex
app-server` subprocess and communicates via newline-delimited JSON-RPC messages
over stdin/stdout.

**Files created/modified:**
- `src/build_your_room/adapters/codex_adapter.py` — adapter factory + live session
- `src/build_your_room/adapters/__init__.py` — re-exports new types
- `tests/test_codex_adapter.py` — 45 tests
- `docs/plans/build-your-room-tasks.md` — marked task 11 complete

## Key design decisions

### JSON-RPC transport as module-level functions
The `_send_message`, `_read_message`, and `_send_and_wait` functions are
module-level rather than methods on the adapter. This keeps them testable in
isolation and avoids coupling the transport logic to any particular session
lifecycle. The `_send_and_wait` function matches responses by request id and
skips interleaved notifications, which is essential since the app-server can
emit async notifications between request/response pairs.

### Context usage estimation
Unlike Claude SDK which provides `get_context_usage()`, Codex app-server has no
direct context window query. We estimate usage from accumulated input/output
token counts against a configurable max (default 128k, overridable via
`config.extra["max_context_tokens"]`). This feeds into the existing
ContextMonitor via `parse_codex_usage(token_input, token_output, max_tokens)` —
note this takes individual counts, not a dict (unlike `parse_claude_usage` which
takes the normalized dict).

### Subprocess cleanup on handshake failure
If the initialize or thread/start handshake fails, the adapter terminates the
subprocess before re-raising. This prevents leaked zombie processes when the
Codex binary isn't installed or returns unexpected errors.

### FakeStdin/FakeStdout test pattern
Tests use `FakeStdin` (records written bytes) and `FakeStdout` (asyncio.Queue of
pre-configured responses) to simulate the subprocess pipe interface without
needing real processes. This allows precise control of the JSON-RPC conversation
and makes tests run in ~0.1s.

## Learnings

1. **parse_codex_usage vs parse_claude_usage signatures differ**: Claude's
   `parse_claude_usage` takes a dict (normalized from SDK camelCase), while
   Codex's `parse_codex_usage` takes `(token_input, token_output, max_tokens)`.
   The adapter's `get_context_usage()` returns a dict for dashboard display, but
   ContextMonitor integration requires calling `parse_codex_usage` with the
   session's accumulated token fields directly.

2. **Corrupted working copy**: The `test_claude_adapter.py` file from the parent
   commit got corrupted in the jj working copy — it was replaced with an older
   version using wrong imports (`build_your_room.claude_adapter` instead of
   `build_your_room.adapters.claude_adapter`). Fixed with
   `jj restore --from @- tests/test_claude_adapter.py`.

3. **asyncio.subprocess.Process typing**: Mock subprocess objects need careful
   setup — `stdin`, `stdout`, `stderr` must be present with the right async
   interfaces. Using `asyncio.Queue` for stdout gives proper async readline
   behavior without real I/O.

## Test count
- New tests: 45
- Total: 388 (343 + 45)
