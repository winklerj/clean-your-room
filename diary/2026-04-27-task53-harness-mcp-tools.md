# Task 53: Wire harness MCP tools into Claude SDK adapter
## Session: 1 | Complexity: medium

### What I did
- Built `src/build_your_room/harness_mcp.py`: a Python in-process SDK MCP
  server (`HARNESS_SERVER_NAME = "harness"`) exposing the six spec-mandated
  tools (`run_tests`, `run_lint`, `run_typecheck`, `start_dev_server`,
  `browser_validate`, `record_browser_artifact`). Each tool is built per
  session via `build_harness_tools(...)` so it closes over the right
  `clone_path`, `allowed_roots`, `CommandRegistry`, and (optional)
  `BrowserRunner`.
- Subprocess-backed tools (`run_tests`/`run_lint`/`run_typecheck`) reuse
  the existing `run_cmd` (sandbox-checked cwd, scrubbed env, `shell=False`)
  via the `CommandRegistry` templates. They never accept arbitrary commands
  from the agent — only template-derived args plus path/pattern hints.
- Browser-backed tools delegate to the injected `BrowserRunner`. Crucially,
  the surface stays stable when no runner is configured: instead of
  silently dropping the tools, they return `is_error=True` text replies
  ("…not configured with a browser runner"). The SDK `allowed_tools`
  filter doesn't have to vary by stage and the agent gets actionable
  feedback if it tries something a code-review session can't do.
- Wired `SessionConfig.mcp_servers: dict[str, Any]` (default `{}`) and
  forwarded it through `ClaudeAgentAdapter.start_session` into
  `ClaudeAgentOptions.mcp_servers`. Added `session_mcp_servers_for(
  agent_type, ...)` to make the per-stage call sites tiny: it returns
  `{}` for non-Claude agents (Codex uses `writableRoots`, not MCP).
- Updated `ToolProfile.all_tools` to qualify harness names as
  `mcp__harness__<n>` (kept `harness_mcp_tools` storage as bare names so
  the property is the only place that knows about the qualification
  rule).
- Wired `session_mcp_servers_for(...)` into validation.py / impl_task.py /
  code_review.py at every `SessionConfig` construction site (including
  the resume_config path in impl_task.py — context rotation must keep the
  same tool surface). Each stage resolves its `command_registry` via
  `command_registry or get_default_command_registry()` so unit tests that
  inject a fixture work unchanged.
- Truncate stdout/stderr at 16 KB head/tail on every tool reply so a
  single tool result can never blow past the context-rotation threshold
  on its own (the `ContextMonitor` rotation still happens, but
  truncating up-front avoids the case where one giant pytest output
  trips the threshold mid-turn).
- 23 new tests in tests/test_harness_mcp.py (21) + tests/test_claude_adapter.py
  (2 for `mcp_servers` forwarding) + tests/test_tool_profiles.py (1
  updated assertion for qualified names). 1262 total tests pass, 0
  warnings, lint clean, mypy clean.

### Learnings
- The Claude Agent SDK exposes `@tool(name, description, input_schema)`
  → `SdkMcpTool[T]` and `create_sdk_mcp_server(name=, tools=)` →
  `McpSdkServerConfig` (a `TypedDict`). `ClaudeAgentOptions.mcp_servers`
  takes `dict[str, McpSdkServerConfig]` keyed by namespace. The agent
  sees tools as `mcp__<server>__<bare>` — so the SDK `allowed_tools`
  filter must list the *qualified* names. Returning the wrong type from
  the SDK call surfaces as a single-line mypy error, which is how I
  caught my placeholder `dict[str, Any]` annotation.
- Tools are dataclasses (`SdkMcpTool`) with a `handler: Callable[[T],
  Awaitable[dict]]` field. That's why testing in-process is trivial:
  `await tool.handler(args)` runs the closure end-to-end without booting
  the SDK. The real plumbing is just registration.
- Codex doesn't use `mcp_servers` — it uses the app-server JSON-RPC
  `writableRoots` sandbox config. The cleanest expression was a tiny
  `session_mcp_servers_for(agent_type, ...)` dispatcher that returns
  `{}` for non-Claude agents. The intent is explicit at the call site
  and the Claude adapter is the only place that has to know about MCP.
- For closure-style tools, the function the `@tool` decorator wraps gets
  *replaced* with an `SdkMcpTool` object. So the symbol you `return` is
  the dataclass, not the original async function — relevant because the
  list comprehension I almost wrote (`return [run_tests, ...]`) works,
  but reusing the names later (e.g. for direct dispatch) would not.
- `BrowserRunner.start_dev_server` accepts `command=None | list[str]`,
  `port: int`, `timeout: float`. The harness tool exposes the same three
  knobs and casts the agent's input (the SDK passes parsed JSON values
  through the input schema, but defensive `int(args.get(...))` /
  `float(args.get(...))` handles wonky inputs without raising).
- Truncating subprocess output at 16 KB *inside* the tool reply (instead
  of relying on `ContextMonitor` to rotate after the fact) is a defense
  in depth: any single pytest run with thousands of failure lines no
  longer threatens to push the session over threshold in one shot.
- The bare-vs-qualified naming split (`harness_mcp_tools` field stores
  bare names; `all_tools` property emits qualified names) lets existing
  tests that assert on the *semantic* tool set keep working untouched,
  while the new SDK-shaped names appear only in the path that hits the
  SDK. Net: one new test broke (the one that asserted `all_tools` was a
  plain concatenation), zero behavioural surprises elsewhere.

### Why this was the right next task
- Spec compliance gap: lines 608-612 mandate this exact tool surface and
  it was missing in functional terms. The previous implementation
  declared the tool *names* in `allowed_tools` but never registered an
  MCP server, so the agent saw "unknown tool" errors if it ever tried
  to call any of them.
- Unblocks Phase 19+ (real Claude session loops): `impl_task` /
  `code_review` / `validation` cannot make verification calls without a
  real tool surface. With harness MCP wired up, every Claude session in
  these stages now has typed access to tests / lint / typecheck / dev
  server / browser scenarios — exactly what the orchestrator promises.
- Small blast radius: changes are additive (new module, one new
  `SessionConfig` field) and the test suite caught every behavioural
  shift before commit. No template/UI churn, no DB migrations.
