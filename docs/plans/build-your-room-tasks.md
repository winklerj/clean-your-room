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
- [x] 35. HTN task tree standalone page — GET /pipelines/{id}/tasks with status/type filters, ancestor-preserving filter, progress bar, filter chips, View full tree link from pipeline detail, 15 new tests (1011 total)
- [x] 36. Stage detail HTMX partial — GET /pipelines/{id}/stages/{stage_id} with per-session logs, output artifact content rendering, review feedback history, context usage bars, HTMX tab click in pipeline detail, partials/stage_detail.html, 20 new tests (1031 total)

## Phase 7: Architecture Alignment (spec-mandated module extractions)

- [x] 37. Extract lease management into lease_manager.py — LeaseManager class with multi-level acquire/release/renew for pipelines/stages/sessions, heartbeat_loop, expiry queries (is_lease_expired, get_expired_running_pipelines, get_live_running_pipelines), release_all_for_pipeline bulk cleanup, LeaseError exception, orchestrator delegated to LeaseManager, 32 new tests (1063 total)
- [x] 38. Extract recovery into recovery.py — RecoveryManager class with reconcile_running_state (startup scan of stale running rows, stage/session/task cleanup), snapshot_dirty_workspace (metadata file creation, DB state update), handle_cancellation (HTN claim release, session/stage cancellation, workspace snapshot), load_visit_counts static helper, orchestrator delegates all recovery operations via _recovery_manager field, injectable pipelines_dir for testing, 33 new tests (1096 total)
- [x] 39. Add StageRunner Protocol and registry-based dispatch — stages/base.py with StageRunnerFn type alias, STAGE_RUNNERS registry dict, register_stage_runner() with conflict detection, get_stage_runner() lookup. All 5 stage runners self-register at module level. Orchestrator refactored from 5-branch if/elif chain to registry dispatch via get_stage_runner(). stages/__init__.py imports trigger registration. Integration tests updated from patch("orchestrator.run_X") to patch.dict("stages.base.STAGE_RUNNERS"). 17 new tests (1113 total)

## Phase 8: Spec Completeness (missing spec features)

- [x] 40. Add pause/kill pipeline buttons to escalation cards — spec line 944 requires Pause pipeline and Kill pipeline action buttons on escalation cards alongside Resolve/Dismiss. Added escalation-pipeline-actions div with Pause (visible for running pipelines) and Kill (visible for running/paused/needs_attention/cancel_requested pipelines) buttons that POST to existing /pipelines/{id}/pause and /pipelines/{id}/kill endpoints. CSS for escalation-pipeline-actions layout. 9 new tests (7 unit + 2 property-based), 1122 total
- [x] 41. Add clone size indicator to pipeline detail page — spec line 966 requires a clone size indicator in the clone management section. Added _get_clone_size() helper (rglob directory walk, human-readable B/KB/MB/GB formatting, graceful None for missing/empty paths), wired into _fetch_pipeline_detail context, clone-size div in pipeline_detail.html between clone path and revision info, CSS for clone-size/clone-size-label/clone-size-value. 9 new tests (6 unit + 2 integration + 1 property-based), 1131 total
- [x] 42. Add stage-graph mini-visualization to pipeline cards — spec line 924 requires pipeline cards to show "Current stage (highlighted in the stage-graph mini-visualization)". Extended _fetch_dashboard_data() to fetch stage_graph_json from pipeline_defs, added _parse_mini_graph_nodes() helper, enriched pipeline data with mini_graph (ordered node list with is_active flag). Added stage-graph-mini div to pipeline_card.html rendering nodes as small connected pills with arrows, active node highlighted via mini-node-active CSS class. Fixed 3 pre-existing Hypothesis test edge cases (surrogate characters in file I/O, whitespace-only form names). 8 new tests (1139 total)

## Phase 9: Code Quality

- [x] 43. Fix Starlette TemplateResponse deprecation warnings — Migrated all 22 TemplateResponse calls across 6 route files (dashboard.py, escalations.py, repos.py, pipelines.py, prompts.py, pipeline_defs.py) from deprecated `TemplateResponse(name, {"request": request, ...})` to new `TemplateResponse(request, name, context={...})` API. Refactored _prompt_context() and _builder_context() helpers to remove request from context dict. Fixed pre-existing Hypothesis flaky test (test_diff_artifact_write_roundtrip: surrogate character exclusion). Warnings reduced from 296 to 4. 1139 total tests (unchanged)
