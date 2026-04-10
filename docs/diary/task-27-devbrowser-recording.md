# Diary

## Goal

Record the implementation of Task 27: Devbrowser recording integration in validation stage — making the BrowserRunner a real bridge to the dev-browser skill with JSON-RPC subprocess protocol, availability detection, graceful degradation, and comprehensive tests.

## Step 1: BrowserRunner enhancement

Enhanced `browser_runner.py` from placeholder stubs to a real dev-browser integration layer:

1. **`_DevBrowserBridge`**: Internal class managing JSON-RPC communication with the dev-browser subprocess over stdin/stdout. Each request is a JSON line with `{id, method, params}`, responses are `{id, result}` or `{id, error}`. Handles timeouts, broken pipes, closed stdout, and invalid JSON gracefully.

2. **`is_available()`**: Static method checking if the dev-browser skill directory exists and contains a recognized entry-point file (`bridge.js`, `server.js`, or `index.js`).

3. **`launch_bridge()`/`close_bridge()`**: Lifecycle management for the bridge subprocess. Idempotent — second launch returns True without relaunching. Handles `FileNotFoundError` when node is not installed.

4. **Bridge-delegating browser tools**: `browser_open`, `browser_run_scenario`, `browser_console_errors`, and `browser_record_artifact` now delegate to the bridge when available, falling back to the original placeholder behavior when not. Bridge errors are caught and logged, never propagated.

5. **Dev server readiness probe**: `start_dev_server()` now TCP-probes the port after launching the subprocess, with configurable timeout. Returns `{"ready": True/False}` alongside the URL and PID.

### Key design decision

All bridge methods catch `DevBrowserBridgeError` and fall back to placeholder behavior. This means the validation stage works identically whether the dev-browser is installed or not — the bridge just adds real browser automation when available.

## Step 2: Validation stage graceful degradation

Updated `validation.py` to:
- Check `BrowserRunner.is_available()` before browser validation phase
- Launch the bridge when available, log fallback mode when not
- Log recording mode ("via bridge" vs "placeholder") in the recording success message
- Bridge launch failure doesn't block validation — proceeds in fallback mode

## Step 3: Comprehensive tests

**46 new tests in `test_browser_runner.py`:**
- 8 availability detection tests (3 positive, 4 negative, 1 property-based)
- 9 bridge JSON-RPC protocol tests (success, incremented IDs, error response, closed stdout, invalid JSON, broken pipe, missing stdin, close lifecycle)
- 5 bridge launch/close lifecycle tests (with skill, without, idempotent, node not found, close)
- 3 browser_open delegation tests (with bridge, without, bridge error)
- 4 browser_run_scenario delegation tests (with bridge, accumulated errors, bridge error, without)
- 3 browser_console_errors delegation tests (with bridge, without, bridge error)
- 3 browser_record_artifact delegation tests (with bridge, without, bridge error)
- 3 dev server readiness tests (already running, timeout, no server)
- 2 cleanup tests (full cleanup, nothing running)
- 2 factory tests (paths, skill path)
- 4 property-based tests (recording metadata keys, availability invariant, scenario fallback, open fallback)

**5 new tests in `test_validation.py`:**
- Bridge launched when available
- Unavailable still validates
- Recording mode logged with bridge
- Recording mode logged placeholder
- Bridge launch failure continues

**Total: 51 new tests (46 + 5), bringing suite to 876.**

## Learnings

1. **Hypothesis + tmp_path**: Function-scoped fixtures (like `tmp_path`) can't be used with `@given` — Hypothesis runs multiple examples within a single test invocation. Use `tempfile.TemporaryDirectory()` inside the test body instead. This was already documented in the memory but worth reinforcing.

2. **AsyncMock.terminate()**: When mocking `asyncio.subprocess.Process`, `terminate()` is a sync method but `AsyncMock` makes it async. The `_DevBrowserBridge.close()` method calls `self.process.terminate()` synchronously, which triggers "coroutine never awaited" warnings with `AsyncMock`. This is a test mock artifact, not a production issue.

3. **Bridge fallback pattern**: The catch-and-fallback pattern in each browser tool method (try bridge, catch error, return fallback) is the right pattern for optional external dependency integration. It keeps the validation stage working regardless of dev-browser availability.
