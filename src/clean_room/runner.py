import asyncio
import os
from pathlib import Path

from clean_room.streaming import LogBuffer


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
                        self.job_id, f"--- Stopped at iteration {iteration} ---"
                    )
                    break

                self.log_buffer.append(
                    self.job_id,
                    f"=== Starting iteration {iteration}/{self.max_iterations} ===",
                )

                output = await self._run_agent_iteration(iteration)

                self.log_buffer.append(
                    self.job_id,
                    f"=== Completed iteration {iteration}/{self.max_iterations} ===",
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
            self.log_buffer.append(self.job_id, f"ERROR: {e}")
            await db.execute(
                "UPDATE jobs SET status='failed', completed_at=datetime('now') WHERE id=?",
                (self.job_id,),
            )
            await db.commit()
            raise
        finally:
            self.log_buffer.close(self.job_id)

    async def _run_agent_iteration(self, iteration: int) -> str:
        """Run a single Claude Agent SDK iteration.

        Uses claude_agent_sdk to run an agent with filesystem access.
        Reads from repo_path, writes specs to specs_path.
        Streams each message to the log buffer in real time.
        """
        from claude_agent_sdk import (
            query, ClaudeAgentOptions, ResultMessage, AssistantMessage, TextBlock,
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

        output_parts = []
        async for message in query(
            prompt=self.prompt,
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
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        output_parts.append(block.text)
                        self.log_buffer.append(self.job_id, block.text)
            elif isinstance(message, ResultMessage) and message.result is not None:
                output_parts.append(message.result)
                self.log_buffer.append(self.job_id, message.result)
        return "\n".join(output_parts) if output_parts else "No output from agent."
