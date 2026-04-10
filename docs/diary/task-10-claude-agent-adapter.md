# Task 10: ClaudeAgentAdapter — live session handles, tool profiles, context monitoring, workspace confinement

## What was done

Created the `adapters` package (`src/build_your_room/adapters/`) with three modules:

1. **`base.py`**: `SessionConfig` frozen dataclass (adapter-agnostic session configuration including model, clone_path, system_prompt, allowed_tools, allowed_roots, context_threshold_pct, output_format, resume_session_id, pipeline/stage IDs). Also hosts the `SessionResult`, `LiveSession`, and `AgentAdapter` Protocol definitions — extracted from orchestrator.py so they're importable without pulling in the full orchestrator.

2. **`claude_adapter.py`**:
   - `ClaudeAgentAdapter`: Factory that creates `ClaudeSDKClient` instances with correct options — model, cwd, permission_mode="acceptEdits", explicit allowed_tools from the stage's ToolProfile, disallowed_tools from DENIED_TOOLS, setting_sources=["project"] to read CLAUDE.md, output_format for structured output, and resume support for context rotation.
   - `ClaudeLiveSession`: Multi-turn session handle wrapping ClaudeSDKClient. `send_turn()` sends a query and drains `receive_response()` collecting text blocks and the final ResultMessage. `get_context_usage()` calls the SDK's context usage endpoint and normalises camelCase keys to snake_case matching ContextMonitor.parse_claude_usage(). `snapshot()` returns a dict suitable for agent_sessions.resume_state_json. `close()` disconnects the client (swallowing errors).
   - `_make_sdk_permission_callback()`: Bridges the sync `make_path_guard` (2-arg `-> bool`) to the SDK's async 3-arg `-> PermissionResult` callback signature. Translates bool results into `PermissionResultAllow`/`PermissionResultDeny`.
   - `ClaudeTurnResult`: Concrete dataclass satisfying the SessionResult protocol with cost/turns/error metadata.

3. **`__init__.py`**: Re-exports the base types for clean imports.

Updated `orchestrator.py` to import `AgentAdapter` from `adapters.base` instead of defining its own Protocol.

37 new tests covering: SessionConfig construction/frozenness, ClaudeTurnResult, permission callback (allow/deny paths, denied tools, non-file tools), ClaudeLiveSession send_turn (text collection, session ID capture, structured output, cost/token accumulation, error results), get_context_usage (normalisation, None handling, error swallowing), snapshot, close, log buffer integration, ClaudeAgentAdapter (option construction, tool passing, resume ID, output format, can_use_tool wiring), and integration tests with ContextMonitor, ToolProfile, and WorkspaceSandbox.

## Learnings

- The Claude Agent SDK's `can_use_tool` callback has an async 3-arg signature `(tool_name, input_data, context) -> PermissionResult`, not the sync 2-arg `-> bool` our `make_path_guard` returns. Bridging requires a thin async wrapper that translates bool results into `PermissionResultAllow`/`PermissionResultDeny`. The underlying sync guard still works fine — the async wrapper just adapts the interface.

- SDK's `get_context_usage()` returns camelCase keys (`totalTokens`, `maxTokens`, `percentage`) while `ContextMonitor.parse_claude_usage` expects `total_tokens`, `max_tokens`. The normalisation layer in `ClaudeLiveSession.get_context_usage()` handles this translation so callers can use either.

- `ClaudeSDKClient.get_context_usage()` is not visible to mypy's type stubs (`attr-defined` error). A targeted `# type: ignore[attr-defined]` is the pragmatic fix — the method exists at runtime.

- Testing with `MagicMock(spec=SomeClass)` makes `isinstance(mock, SomeClass)` return True, which is essential when the production code uses isinstance checks against SDK types (AssistantMessage, ResultMessage, TextBlock). Custom test dataclasses won't pass isinstance checks.

- The `receive_response()` method returns an async generator. Mock clients must assign an actual async generator function (not a regular generator) for `async for` to work.

## Test count

Before: 306 tests
After: 343 tests (+37)
