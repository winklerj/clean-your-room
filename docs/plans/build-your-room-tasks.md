# Build Your Room — Implementation Task List

Tracking progress on the build-your-room spec implementation plan.

## Phase 1: Foundation (scaffold + DB + core loop)

- [x] 1. Fork clean-your-room → build-your-room, strip specs-monorepo and GitHub-specific code
- [x] 2. New PostgreSQL schema + import path from old SQLite tables (including HTN task tables)
- [x] 3. PipelineOrchestrator skeleton with stage-graph dispatch, durable leases, dirty-workspace recovery, and startup reconciliation
- [x] 4. CloneManager for repo cloning, workspace refs, cleanup, and reset-to-head behavior
- [x] 5. Sandbox/path guard abstraction + per-stage tool profiles reused by adapters and verifiers
- [x] 6. Command-template registry for repo-standard `uv run` verification commands
- [x] 7. ContextMonitor as a reusable hook
- [x] 8. Config module with all env vars
- [x] 9. HTNPlanner: task graph CRUD, atomic claims, readiness propagation, postcondition verification

## Phase 2: Agent adapters

- [x] 10. ClaudeAgentAdapter with live session handles, explicit tool profiles, context monitoring, and workspace confinement
- [x] 11. CodexAppServerAdapter with stdio JSON-RPC protocol, persistent threads, and workspace-write sandboxing
- [x] 12. Review loop logic with structured approval parsing, same-session continuation, and restart fallback
- [x] 13. Adapter unit tests with mocked agent responses (82 tests: 37 Claude + 45 Codex)

## Phase 3: Stage runners

- [x] 14. SpecAuthorStage + review loop integration
- [x] 15. ImplPlanStage + review loop integration + HTN task graph population from structured output
- [x] 16. ImplTaskStage with atomic HTN task claims, same-task context rotation, and postcondition verification
- [x] 17. CodeReviewStage with full-head diff review and bug-fix loop
- [x] 18. ValidationStage with typed browser tools and harness-owned dev-browser integration

## Phase 4: Dashboard

- [x] 19. Dashboard template with pipeline cards grid + HTN progress indicators
- [x] 20. Escalation queue page
- [x] 21. Pipeline detail page with stage-graph viz, HTN task tree, lease health, dirty-snapshot visibility, and live logs
- [x] 22. Pipeline builder form with explicit node/edge editing
- [x] 23. Prompt management (extend existing)
- [x] 24. Clone cleanup buttons (per-pipeline and bulk)

## Phase 5: Polish + validation

- [x] 25. Property-based tests for orchestrator state machine, stage-transition guards, and HTN claims (Hypothesis)
- [x] 26. Integration tests with mock adapters + `pytest-postgresql`
- [x] 27. Devbrowser recording integration in validation stage
- [x] 28. Documentation (README, CLAUDE.md, AGENTS.md)
- [x] 29. Default prompt templates for all stage types

## Phase 6: API + Operational Completeness

- [x] 30. JSON API for programmatic access (routes/api.py) — 10 endpoints, 36 tests (924 total)
- [x] 31. Pipeline creation form and lifecycle control HTML routes — GET /pipelines/new, POST /pipelines, cancel/kill/pause/resume HTML routes, action buttons in detail page, 28 tests (952 total)
- [x] 32. SSE streaming endpoints — GET /pipelines/{id}/stream (pipeline-scoped) and GET /sessions/{id}/stream (session-scoped) via LogBuffer + sse-starlette EventSourceResponse, pipeline detail template upgraded to SSE-first with HTMX polling fallback, 17 tests (969 total)
- [x] 33. HTN decision task resolution HTML endpoint — POST /pipelines/{id}/tasks/{task_id}/resolve with inline form, validation guards, readiness propagation, 11 tests (980 total)
- [x] 34. Repo management page — GET /repos dedicated repo list with pipeline counts/status per repo, latest pipeline info, show/hide archived filter, enriched repo detail with pipeline history table and "New Pipeline" links, Repos nav link, 16 new tests (996 total)
