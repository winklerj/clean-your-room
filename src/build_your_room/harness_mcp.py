"""Harness MCP server: typed tools the Claude SDK exposes to agents.

The agent never invokes ``Bash`` or shell. For implementation, code-review,
bug-fix, and validation stages the harness publishes a small set of typed
tools through an in-process MCP server registered with the Claude Agent
SDK. Each tool wraps an app-owned :class:`CommandRegistry` template or a
:class:`BrowserRunner` operation; both already contain workspace
sandboxing, scrubbed environments, and command-template policy.

Tool surface (matches spec lines 608-612)::

    run_tests, run_lint, run_typecheck,
    start_dev_server, browser_validate, record_browser_artifact

Wire-up flow:

1. A stage (impl_task / code_review / validation) calls
   :func:`build_session_mcp_servers` with the per-pipeline ``clone_path``,
   ``allowed_roots``, command registry, and (optional) browser runner.
2. The returned dict is set on :class:`SessionConfig.mcp_servers`.
3. :class:`ClaudeAgentAdapter` forwards the dict into
   :class:`ClaudeAgentOptions.mcp_servers`. Tools become reachable to the
   agent under qualified names like ``mcp__harness__run_tests``.
4. The same qualified names appear in :class:`ToolProfile.all_tools` so
   the Claude SDK ``allowed_tools`` filter accepts the calls.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from build_your_room.browser_runner import BrowserRunner, BrowserRunnerError
from build_your_room.command_registry import (
    CommandRegistry,
    expand_paths,
    expand_test_targets,
    run_cmd,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

#: MCP server name registered with the Claude SDK. Tools are reachable as
#: ``mcp__{HARNESS_SERVER_NAME}__{tool_name}``.
HARNESS_SERVER_NAME = "harness"

#: Bare semantic names of every tool the harness publishes. Kept in spec
#: order for readability.
HARNESS_TOOL_NAMES: tuple[str, ...] = (
    "run_tests",
    "run_lint",
    "run_typecheck",
    "start_dev_server",
    "browser_validate",
    "record_browser_artifact",
)


def qualified_tool_name(bare_name: str) -> str:
    """Return the SDK-recognised tool name for a bare harness tool."""
    return f"mcp__{HARNESS_SERVER_NAME}__{bare_name}"


def qualified_tool_names() -> tuple[str, ...]:
    """All harness tool names in the SDK-qualified form."""
    return tuple(qualified_tool_name(n) for n in HARNESS_TOOL_NAMES)


# ---------------------------------------------------------------------------
# Output shaping
# ---------------------------------------------------------------------------

# Subprocess output can be large. Cap each tool's reply to keep the agent
# context budget under control. ContextMonitor-driven rotation still works
# but truncating up-front avoids one giant tool result blowing past the
# threshold mid-turn.
_MAX_OUTPUT_CHARS = 16_000


def _truncate(text: str, limit: int = _MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return (
        f"{head}\n\n[...truncated {len(text) - limit} chars...]\n\n{tail}"
    )


def _format_command_result(
    name: str,
    args: list[str],
    rc: int,
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    """Shape a subprocess result into an MCP tool reply."""
    body = (
        f"$ {' '.join(args)}\n"
        f"exit_code={rc}\n"
        f"--- stdout ---\n{_truncate(stdout)}\n"
        f"--- stderr ---\n{_truncate(stderr)}"
    )
    return {
        "content": [{"type": "text", "text": body}],
        "is_error": rc != 0,
    }


def _error_reply(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "is_error": True,
    }


# ---------------------------------------------------------------------------
# Tool factory
# ---------------------------------------------------------------------------


def build_harness_tools(
    *,
    clone_path: str,
    allowed_roots: list[str],
    command_registry: CommandRegistry,
    browser_runner: BrowserRunner | None = None,
) -> list[SdkMcpTool[Any]]:
    """Build the per-session list of harness MCP tools.

    All tools close over ``clone_path``, ``allowed_roots``, the registry,
    and an optional browser runner. ``run_cmd`` already enforces the
    workspace sandbox and uses a scrubbed environment, so command
    templates remain authoritative.

    The ``browser_runner`` parameter is optional. When omitted, the three
    browser-oriented tools still exist but return an ``is_error`` reply
    explaining the harness is in a no-browser configuration. This keeps
    the tool surface stable for agents regardless of whether the dev
    browser bridge is wired in.
    """
    roots = [Path(r) for r in allowed_roots]

    @tool(
        "run_tests",
        "Run the repo-standard test suite via the harness command "
        "template (e.g. `uv run pytest`). Optional `pattern` filters to "
        "matching test files; optional `paths` is an explicit list of "
        "test files. Returns combined stdout/stderr and exit code.",
        {
            "pattern": str,
            "paths": list,
        },
    )
    async def run_tests(args: dict[str, Any]) -> dict[str, Any]:
        template = command_registry.get("tests_pass")
        if template is None:
            return _error_reply("No tests_pass command template registered")
        targets = expand_test_targets(args)
        cmd = template.build_args(targets)
        rc, stdout, stderr = await run_cmd(
            cmd, clone_path, allowed_roots=roots,
        )
        return _format_command_result("run_tests", cmd, rc, stdout, stderr)

    @tool(
        "run_lint",
        "Run the repo-standard linter via the harness command template "
        "(e.g. `uv run ruff check`). Optional `scope` is a single path/"
        "directory; optional `paths` is an explicit list. Returns "
        "combined stdout/stderr and exit code.",
        {
            "scope": str,
            "paths": list,
        },
    )
    async def run_lint(args: dict[str, Any]) -> dict[str, Any]:
        template = command_registry.get("lint_clean")
        if template is None:
            return _error_reply("No lint_clean command template registered")
        paths = expand_paths(args)
        cmd = template.build_args(paths)
        rc, stdout, stderr = await run_cmd(
            cmd, clone_path, allowed_roots=roots,
        )
        return _format_command_result("run_lint", cmd, rc, stdout, stderr)

    @tool(
        "run_typecheck",
        "Run the repo-standard type checker via the harness command "
        "template (e.g. `uv run mypy src/ --ignore-missing-imports`). "
        "Returns combined stdout/stderr and exit code.",
        {},
    )
    async def run_typecheck(args: dict[str, Any]) -> dict[str, Any]:
        template = command_registry.get("type_check")
        if template is None:
            return _error_reply("No type_check command template registered")
        cmd = template.build_args()
        rc, stdout, stderr = await run_cmd(
            cmd, clone_path, allowed_roots=roots,
        )
        return _format_command_result("run_typecheck", cmd, rc, stdout, stderr)

    @tool(
        "start_dev_server",
        "Start the repo's dev-server inside the pipeline clone via the "
        "harness browser runner. Optional `command` overrides the default "
        "(`npm run dev`); optional `port` (default 3000) and `timeout` "
        "(seconds, default 30) shape the readiness probe. Returns the "
        "URL, pid, and ready flag.",
        {
            "command": list,
            "port": int,
            "timeout": float,
        },
    )
    async def start_dev_server(args: dict[str, Any]) -> dict[str, Any]:
        if browser_runner is None:
            return _error_reply(
                "Dev server unavailable: this stage was not configured "
                "with a browser runner."
            )
        command = args.get("command")
        port = int(args.get("port", 3000))
        timeout = float(args.get("timeout", 30.0))
        try:
            result = await browser_runner.start_dev_server(
                command=command, port=port, timeout=timeout,
            )
        except BrowserRunnerError as exc:
            return _error_reply(f"start_dev_server failed: {exc}")
        text = (
            f"Dev server: url={result['url']} pid={result['pid']} "
            f"ready={result['ready']}"
        )
        return {
            "content": [{"type": "text", "text": text}],
            "is_error": not result.get("ready", False),
        }

    @tool(
        "browser_validate",
        "Run a browser scenario against the dev-server through the "
        "harness's dev-browser bridge. The `scenario` string is "
        "interpreted by the bridge; when no bridge is present a "
        "placeholder pass result is returned. Reports passed/failed plus "
        "any console errors collected.",
        {"scenario": str},
    )
    async def browser_validate(args: dict[str, Any]) -> dict[str, Any]:
        if browser_runner is None:
            return _error_reply(
                "Browser validation unavailable: this stage was not "
                "configured with a browser runner."
            )
        scenario = str(args.get("scenario", "")).strip()
        if not scenario:
            return _error_reply("browser_validate requires a non-empty scenario")
        result = await browser_runner.browser_run_scenario(scenario)
        body_lines = [
            f"scenario={scenario!r}",
            f"passed={result.passed}",
            f"console_errors={len(result.console_errors)}",
        ]
        if result.console_errors:
            body_lines.append("--- console_errors ---")
            for err in result.console_errors[:50]:
                body_lines.append(f"- {err}")
        if result.screenshot_path:
            body_lines.append(f"screenshot={result.screenshot_path}")
        if result.details:
            body_lines.append(f"details={result.details}")
        return {
            "content": [{"type": "text", "text": "\n".join(body_lines)}],
            "is_error": not result.passed,
        }

    @tool(
        "record_browser_artifact",
        "Capture a recording (default GIF) of the current browser state "
        "and persist it under the pipeline's artifacts/validation/ "
        "directory. Used by validation when `record_on_success` is on. "
        "`name` and `format` are optional.",
        {"name": str, "format": str},
    )
    async def record_browser_artifact(args: dict[str, Any]) -> dict[str, Any]:
        if browser_runner is None:
            return _error_reply(
                "Recording unavailable: this stage was not configured "
                "with a browser runner."
            )
        name = str(args.get("name", "validation_recording")).strip() or (
            "validation_recording"
        )
        fmt = str(args.get("format", "gif")).strip() or "gif"
        result = await browser_runner.browser_record_artifact(
            name=name, format=fmt,
        )
        text = (
            f"Recorded {result.get('format', fmt)} artifact "
            f"name={result.get('name', name)} path={result.get('path', '')}"
        )
        return {
            "content": [{"type": "text", "text": text}],
            "is_error": False,
        }

    return [
        run_tests,
        run_lint,
        run_typecheck,
        start_dev_server,
        browser_validate,
        record_browser_artifact,
    ]


def build_harness_mcp_server(
    *,
    clone_path: str,
    allowed_roots: list[str],
    command_registry: CommandRegistry,
    browser_runner: BrowserRunner | None = None,
) -> McpSdkServerConfig:
    """Build the SDK MCP server config for the harness namespace."""
    tools = build_harness_tools(
        clone_path=clone_path,
        allowed_roots=allowed_roots,
        command_registry=command_registry,
        browser_runner=browser_runner,
    )
    return create_sdk_mcp_server(name=HARNESS_SERVER_NAME, tools=tools)


def build_session_mcp_servers(
    *,
    clone_path: str,
    allowed_roots: list[str],
    command_registry: CommandRegistry,
    browser_runner: BrowserRunner | None = None,
) -> dict[str, Any]:
    """Return the ``{server_name: config}`` map for ``SessionConfig.mcp_servers``.

    Stages call this to enable the harness namespace for a Claude session.
    The Claude adapter forwards the dict directly into
    :class:`ClaudeAgentOptions.mcp_servers`.
    """
    return {
        HARNESS_SERVER_NAME: build_harness_mcp_server(
            clone_path=clone_path,
            allowed_roots=allowed_roots,
            command_registry=command_registry,
            browser_runner=browser_runner,
        ),
    }


def session_mcp_servers_for(
    agent_type: str,
    *,
    clone_path: str,
    allowed_roots: list[str],
    command_registry: CommandRegistry,
    browser_runner: BrowserRunner | None = None,
) -> dict[str, Any]:
    """Build harness MCP servers for Claude sessions; return ``{}`` for others.

    The Claude SDK is the only adapter that reads ``mcp_servers``; Codex
    relies on a separate ``writableRoots`` sandbox protocol. Building the
    server is cheap, but skipping it for non-Claude agents keeps the
    intent explicit at the call site.
    """
    if agent_type != "claude":
        return {}
    return build_session_mcp_servers(
        clone_path=clone_path,
        allowed_roots=allowed_roots,
        command_registry=command_registry,
        browser_runner=browser_runner,
    )
