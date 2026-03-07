# Architecture and Design Decisions

Clean Your Room is a FastAPI web application that orchestrates iterative AI agents (via the Claude Agent SDK) against GitHub repositories to generate formal clean room specifications -- documents that capture provable properties, purity boundaries, and verification tooling for a codebase. This document explains *why* the system is built the way it is, the trade-offs involved, and how the pieces fit together.

---

## Async-First Design

Every I/O-bound operation in Clean Your Room is asynchronous. Database calls go through aiosqlite, git operations run as async subprocesses via `asyncio.create_subprocess_exec`, and background jobs are launched with `asyncio.create_task()`. This is not a stylistic preference -- it is a structural requirement.

FastAPI is async-native, and Clean Your Room's workload involves long-running agent iterations, real-time SSE streams to the browser, and concurrent database reads and writes. A synchronous design would mean that a single multi-minute agent run blocks the entire server, preventing other users from viewing job logs or submitting new work. The async model allows the event loop to interleave these concerns naturally: while one job awaits an agent response, another job's SSE stream can push log lines, and a third request can query the database for the dashboard.

The choice to run git commands as async subprocesses (rather than using a Python git library like GitPython) deserves mention. Subprocess execution keeps the git logic simple and predictable -- the same commands a developer would run manually -- while `asyncio.create_subprocess_exec` ensures these potentially slow operations (cloning large repos, pulling updates) never block the event loop.

---

## In-Memory Pub/Sub for Real-Time Streaming

The `LogBuffer` class is the backbone of Clean Your Room's real-time log display. It maintains an in-memory history of messages per job and a set of `asyncio.Queue` subscribers. When a job runner appends a log line, every active subscriber receives it immediately through their queue. The `subscribe()` method is an async generator, which makes it a natural fit for SSE endpoints -- the generator yields messages as they arrive, and FastAPI's `EventSourceResponse` translates each yield into a server-sent event.

This design reflects a deliberate choice among several alternatives.

**Why not poll the database?** The job_logs table does persist every iteration's output, so one could imagine an SSE endpoint that periodically queries for new rows. But polling introduces latency (you only see updates at the poll interval) and generates unnecessary database load, especially when multiple clients watch the same job. The in-memory pub/sub delivers messages with effectively zero latency and zero DB overhead for streaming.

**Why not Redis or an external message queue?** Clean Your Room is designed for single-process, single-instance deployment. Adding Redis would introduce an external dependency, operational complexity (another process to manage, another failure mode to handle), and configuration surface area -- all for a pub/sub pattern that `asyncio.Queue` handles elegantly within a single process. The simplicity argument is strong here: fewer moving parts means fewer things that can break.

**The trade-off is explicit.** In-memory state is lost on process restart. If the server crashes mid-job, active subscribers lose their connection, and the in-memory log history disappears. This is acceptable because the durable record lives in the `job_logs` table. When a client reconnects or loads the job viewer page, it reads persisted logs from the database. The in-memory layer is an acceleration cache for live streaming, not the source of truth.

The `close()` method on LogBuffer sends a `None` sentinel to all subscribers when a job finishes, ensuring that SSE connections terminate cleanly rather than hanging indefinitely.

---

## Specs Monorepo Pattern

When a job completes, its generated specifications are copied from the individual repository clone into a central directory at `~/.clean-room/specs-monorepo/`. Each repository's specs land in a subdirectory named by slug (the `org--repo` format, e.g., `anthropics--claude-code`), and the changes are automatically committed to a local git repository.

This pattern exists to solve a specific problem: understanding how specifications evolve over time across all tracked repositories. Without the monorepo, you would need to visit each individual repo clone to inspect its specs, and there would be no unified history of when specs were created or improved. The monorepo provides a single `git log` that shows the full timeline of spec generation activity.

The slug naming convention (double-dash separator) avoids ambiguity with org or repo names that might contain single dashes, while remaining filesystem-friendly. The automatic commit on job completion means the monorepo's git history directly mirrors the job history -- each commit corresponds to a completed (or partially completed) job run.

An alternative approach would be to store specs only in the database. But specs are text files that benefit from git's diffing and history capabilities, and keeping them as files makes it easy to browse, search, and use them with standard tools outside of Clean Your Room itself.

---

## HTMX + Server-Side Templates

Clean Your Room uses Jinja2 templates rendered on the server, enhanced with HTMX for dynamic interactions. Partial templates (like `prompt_form.html` and `prompt_row.html`) enable inline editing without full page reloads. The SSE extension for HTMX powers the real-time log display in the job viewer.

**Why not a single-page application framework?** The decision comes down to complexity budget. Clean Your Room is a tool for developers, not a consumer product with complex client-side state. The interactions are straightforward: submit forms, display lists, stream logs. HTMX handles these patterns with a few HTML attributes rather than requiring a JavaScript build pipeline, a client-side router, state management, and API serialization layers.

Server-side rendering also means the server is the single source of truth for what the user sees. There is no client-side state that can drift out of sync with the database. When you submit a form to create a job, the server redirects to the job viewer page, which renders the current state from the database. This request-response simplicity is harder to achieve with a SPA, where you often end up maintaining parallel representations of the same data.

The partial template pattern deserves specific attention. Rather than rendering entire pages for small updates, HTMX can request a fragment (e.g., a single table row after editing a prompt) and swap it into the DOM. This gives the responsiveness of a SPA for common interactions while keeping all rendering logic on the server.

---

## Job Lifecycle and Cancellation

A job in Clean Your Room moves through a defined lifecycle: `pending` (created but not yet started), `running` (agent iterations in progress), and then one of `completed`, `stopped`, or `failed`. The system tracks running jobs through two dictionaries: `active_jobs` maps job IDs to `asyncio.Event` objects for cancellation signaling, and `running_tasks` maps job IDs to `asyncio.Task` references.

Cancellation is cooperative, not preemptive. When a user clicks "Stop," the server sets the job's `asyncio.Event`. The `JobRunner` checks this event at the boundary between iterations -- after one iteration finishes and before the next begins. If the event is set, the runner logs the cancellation and exits the loop.

**Why cooperative cancellation?** An agent iteration involves a call to the Claude Agent SDK, which may itself involve multiple API requests, tool use, and reasoning steps. Interrupting this mid-execution would leave the agent's work in an undefined state -- partially written files, incomplete analysis, broken specs. By checking at iteration boundaries, the system ensures that each completed iteration represents a coherent unit of work. The specs from a stopped job may be incomplete (covering fewer aspects of the codebase), but they will not be corrupted.

The `restart_job` endpoint creates a new job with the same parameters rather than resuming the old one. This is a deliberate simplification: each job is an independent run with its own log history. There is no need to track "where we left off" or handle partially completed iteration state.

---

## Iterative Agent Model

The `JobRunner` executes a loop of 1 to `max_iterations` Claude Agent SDK calls. Each iteration receives the same prompt and repo context, and its output is both persisted to the `job_logs` table and streamed to any SSE subscribers. On successful completion, specs are copied to the monorepo.

**Why iterative rather than single-shot?** Formal specification generation benefits from multiple passes over the same codebase. A single agent invocation might produce an initial draft of one specification, but subsequent iterations can identify gaps, refine property definitions, add missing purity boundaries, or create entirely new spec files for other parts of the system. The iterative model mirrors how a human would approach this work: draft, review, improve.

The default of 20 iterations reflects a balance. Too few iterations and the specs remain shallow; too many and you burn API credits on diminishing returns. Making this configurable per job allows users to experiment -- running a quick 3-iteration pass to preview what the agent will focus on, then a longer run for thorough coverage.

Each iteration is logged independently, which provides transparency into the agent's reasoning process. Users can watch the SSE stream to see whether the agent is making productive progress or spinning in circles, and stop the job early if needed.

---

## Database Choices

Clean Your Room uses SQLite as its database, accessed through aiosqlite with WAL (Write-Ahead Logging) mode enabled. Foreign keys are enforced via PRAGMA. Timestamps are stored as TEXT columns using SQLite's `datetime('now')` function.

**Why SQLite?** The application is designed for single-instance deployment -- one developer or small team running it locally or on a single server. SQLite requires zero configuration, has no separate server process, and stores everything in a single file (`~/.clean-room/clean_room.db`). For this deployment model, a client-server database like PostgreSQL would add operational overhead without providing meaningful benefits.

WAL mode is essential given the async architecture. Without it, SQLite's default journal mode would serialize all access, meaning a long-running write (like updating job status) could block reads (like loading the dashboard). WAL mode allows concurrent readers during writes, which aligns with the application's pattern of frequent reads (page loads, status checks) alongside periodic writes (job progress updates).

The choice to store timestamps as TEXT rather than INTEGER (Unix timestamps) reflects SQLite's type flexibility and prioritizes human readability. When inspecting the database directly with `sqlite3`, text timestamps like `2026-03-07 14:30:00` are immediately understandable.

Default prompt seeding is idempotent -- the `init_db` function only inserts the built-in "Create Spec" and "Improve Spec" prompts when the prompts table is empty. This means the first run populates useful defaults, but subsequent runs (or runs after the user has added their own prompts) leave the table untouched. If a user deletes all prompts and restarts, the defaults reappear -- a reasonable behavior that avoids a permanently empty prompt list.

---

## How the Pieces Connect

These design decisions are not independent -- they reinforce each other. The async-first design enables the in-memory pub/sub, which enables real-time SSE streaming, which makes the HTMX frontend feel responsive without client-side complexity. SQLite with WAL mode supports the concurrent read/write pattern that emerges from background jobs updating the database while the dashboard queries it. The iterative agent model creates the need for streaming (long-running jobs that produce incremental output), and the specs monorepo gives that incremental output a durable, versioned home.

The unifying theme is **appropriate simplicity**. Each component uses the simplest approach that meets its requirements -- asyncio.Queue instead of Redis, SQLite instead of PostgreSQL, HTMX instead of React, cooperative cancellation instead of process isolation. This is not accidental frugality but a deliberate design strategy for a tool that should be easy to run, easy to understand, and easy to modify.
