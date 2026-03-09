import asyncio
import html
import os
from pathlib import Path
from typing import Any

from clean_room.streaming import LogBuffer


def _fmt(css_class: str, label: str, content: str) -> str:
    """Wrap a log message in a styled HTML div."""
    safe = html.escape(content)
    return f'<div class="log-msg {css_class}"><span class="log-label">{label}</span>{safe}</div>'


class JobRunner:
    """Runs iterative Claude Agent SDK loops for a job."""

    def __init__(
        self,
        job_id: int,
        repo_path: Path,
        specs_path: Path,
        prompt: str,
        max_iterations: int,
        log_buffer: LogBuffer,
        cancel_event: asyncio.Event,
    ):
        self.job_id = job_id
        self.repo_path = repo_path
        self.specs_path = specs_path
        self.prompt = prompt
        self.max_iterations = max_iterations
        self.log_buffer = log_buffer
        self.cancel_event = cancel_event

    async def run(self, db) -> None:
        """Execute the iteration loop."""
        try:
            for iteration in range(1, self.max_iterations + 1):
                if self.cancel_event.is_set():
                    self.log_buffer.append(
                        self.job_id,
                        _fmt("log-iteration", "", f"Stopped at iteration {iteration}"),
                    )
                    break

                self.log_buffer.append(
                    self.job_id,
                    _fmt("log-iteration", "", f"Iteration {iteration}/{self.max_iterations}"),
                )

                output = await self._run_agent_iteration(iteration)

                self.log_buffer.append(
                    self.job_id,
                    _fmt("log-iteration", "", f"Completed iteration {iteration}/{self.max_iterations}"),
                )

                await db.execute(
                    "INSERT INTO job_logs (job_id, iteration, content) VALUES (?, ?, ?)",
                    (self.job_id, iteration, output),
                )
                await db.execute(
                    "UPDATE jobs SET current_iteration=? WHERE id=?",
                    (iteration, self.job_id),
                )
                await db.commit()
        except Exception as e:
            self.log_buffer.append(self.job_id, _fmt("log-error", "ERROR", str(e)))
            await db.execute(
                "UPDATE jobs SET status='failed', completed_at=datetime('now') WHERE id=?",
                (self.job_id,),
            )
            await db.commit()
            raise

    def _make_path_guard(self):
        """Return a can_use_tool callback that restricts file access."""
        from claude_agent_sdk import (
            PermissionResultAllow, PermissionResultDeny, ToolPermissionContext,
        )

        repo = str(self.repo_path.resolve())
        specs = str(self.specs_path.resolve())

        # Tools that take a file/directory path argument
        path_params = {
            "Read": "file_path",
            "Write": "file_path",
            "Edit": "file_path",
            "Glob": "path",
            "Grep": "path",
        }

        async def guard(
            tool_name: str, tool_input: dict[str, Any], ctx: ToolPermissionContext,
        ) -> PermissionResultAllow | PermissionResultDeny:
            param = path_params.get(tool_name)
            if param is None:
                return PermissionResultAllow()
            raw = tool_input.get(param)
            if raw is None:
                # Glob/Grep default to cwd (specs_path) when path is omitted
                return PermissionResultAllow()
            resolved = str(Path(raw).resolve())
            if resolved.startswith(repo) or resolved.startswith(specs):
                return PermissionResultAllow()
            return PermissionResultDeny(
                message=f"Access denied: path {raw} is outside the allowed directories",
            )

        return guard

    async def _run_agent_iteration(self, iteration: int) -> str:
        """Run a single Claude Agent SDK iteration.

        Uses claude_agent_sdk to run an agent with filesystem access.
        Reads from repo_path, writes specs to specs_path.
        Streams each message to the log buffer in real time.
        """
        from claude_agent_sdk import (
            query, ClaudeAgentOptions, ResultMessage, AssistantMessage,
            UserMessage, SystemMessage, TextBlock, ThinkingBlock,
            ToolUseBlock, ToolResultBlock,
            TaskStartedMessage, TaskProgressMessage, TaskNotificationMessage,
        )

        clean_env = {
            k: v for k, v in os.environ.items() if k != "CLAUDECODE"
        }

        self.specs_path.mkdir(parents=True, exist_ok=True)

        system = (
            f"You are analyzing the repository at {self.repo_path} (read-only reference).\n"
            f"Write ALL output files to {self.specs_path} (your working directory).\n"
            f"Read existing specs from {self.specs_path} to see what has already been created.\n"
            "IMPORTANT: You MUST write all spec files to your working directory ONLY.\n"
            "Do NOT create files in the repository or any other directory."
        )

        async def _prompt_iter():
            yield {"type": "user", "message": {"role": "user", "content": self.prompt}}

        output_parts = []
        async for message in query(
            prompt=_prompt_iter(),
            options=ClaudeAgentOptions(
                cwd=str(self.specs_path),
                model="claude-sonnet-4-6",
                system_prompt=system,
                add_dirs=[str(self.repo_path)],
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
                permission_mode="acceptEdits",
                max_turns=50,
                setting_sources=["project"],
                env=clean_env,
                can_use_tool=self._make_path_guard(),
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ThinkingBlock):
                        formatted = _fmt("log-thinking", "Thinking", block.thinking)
                        output_parts.append(formatted)
                        self.log_buffer.append(self.job_id, formatted)
                    elif isinstance(block, TextBlock):
                        formatted = _fmt("log-text", "Assistant", block.text)
                        output_parts.append(formatted)
                        self.log_buffer.append(self.job_id, formatted)
                    elif isinstance(block, ToolUseBlock):
                        formatted = _fmt("log-tool-call", f"Tool: {html.escape(block.name)}", str(block.input))
                        output_parts.append(formatted)
                        self.log_buffer.append(self.job_id, formatted)
            elif isinstance(message, UserMessage):
                for block in (message.content if isinstance(message.content, list) else []):
                    if isinstance(block, ToolResultBlock):
                        if block.is_error:
                            formatted = _fmt("log-tool-error", "Tool Error", str(block.content))
                        else:
                            formatted = _fmt("log-tool-result", "Tool Result", str(block.content))
                        output_parts.append(formatted)
                        self.log_buffer.append(self.job_id, formatted)
            elif isinstance(message, SystemMessage):
                formatted = _fmt("log-system", f"System ({html.escape(str(message.subtype))})", str(message.data))
                output_parts.append(formatted)
                self.log_buffer.append(self.job_id, formatted)
            elif isinstance(message, TaskStartedMessage):
                formatted = _fmt("log-task", "Task Started", f"{message.task_id}: {message.description}")
                output_parts.append(formatted)
                self.log_buffer.append(self.job_id, formatted)
            elif isinstance(message, TaskProgressMessage):
                desc = f"{message.task_id}: {message.description}"
                if message.last_tool_name:
                    desc += f" (tool: {message.last_tool_name})"
                formatted = _fmt("log-task", "Task Progress", desc)
                output_parts.append(formatted)
                self.log_buffer.append(self.job_id, formatted)
            elif isinstance(message, TaskNotificationMessage):
                formatted = _fmt("log-task", f"Task {message.status.title()}", f"{message.task_id}: {message.summary}")
                output_parts.append(formatted)
                self.log_buffer.append(self.job_id, formatted)
            elif isinstance(message, ResultMessage) and message.result is not None:
                formatted = _fmt("log-result", "Result", message.result)
                output_parts.append(formatted)
                self.log_buffer.append(self.job_id, formatted)
        return "\n".join(output_parts) if output_parts else "No output from agent."
