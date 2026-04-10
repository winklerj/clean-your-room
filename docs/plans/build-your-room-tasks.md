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

- [ ] 25. Property-based tests for orchestrator state machine, stage-transition guards, and HTN claims (Hypothesis)
- [ ] 26. Integration tests with mock adapters + `pytest-postgresql`
- [ ] 27. Devbrowser recording integration in validation stage
- [ ] 28. Documentation (README, CLAUDE.md, AGENTS.md)
- [ ] 29. Default prompt templates for all stage types
