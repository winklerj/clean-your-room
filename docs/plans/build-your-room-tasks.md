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
- [x] 44. Fix remaining 4 test warnings to achieve zero-warning test suite — Fixed 3 RuntimeWarning "coroutine never awaited" in test_browser_runner.py by making process.terminate()/kill() use MagicMock instead of AsyncMock (asyncio.subprocess.Process.terminate/kill are synchronous methods). Fixed 1 PytestUnraisableExceptionWarning (subprocess transport GC after event loop closes) via targeted filterwarning in pyproject.toml matching BaseSubprocessTransport. Warnings reduced from 4 to 0. 1139 total tests (unchanged)

## Phase 10: Spec Route Completeness

- [x] 45. Add GET /repos/new route and new_repo.html template — Spec line 1143 requires `GET /repos/new → add repo form` but only POST /repos existed. Added new_repo_form() route handler in repos.py, new_repo.html template with name/local_path/git_url/default_branch fields reusing .new-pipeline-form CSS, extended CSS for text input styling. repos.html already linked to /repos/new (was a 404). 6 new tests (form rendering, required fields, form action, cancel link, default branch value, submit flow). 1145 total tests

## Phase 11: Route Conflict Fixes

- [x] 46. Fix duplicate /repos/new route shadowing — dashboard.py had a stale GET /repos/new handler (returning add_repo.html) that shadowed the correct repos.py route (returning new_repo.html) because dashboard_router was included before repos_router. Removed stale route from dashboard.py, deleted orphaned add_repo.html template, added test_new_repo_form_uses_styled_template asserting new-pipeline-form/btn-cancel/field-label CSS classes. 1 new test. 1146 total tests

## Phase 12: Spec Completeness — Dashboard Polish

- [x] 47. Add context usage chart over time to stage detail — spec line 954 requires "Context usage chart over time" as a distinct section in stage tabs. Added _build_context_chart() helper in routes/pipelines.py (pre-computes SVG coordinates: bars per session with non-null context_usage_pct, grid lines at 0/25/50/75/100%, dashed threshold line from pipeline config_json, color-coded bars matching existing ok/warn/high scheme). Enhanced _fetch_stage_detail() to query pipeline config_json for context_threshold_pct and compute chart data. Added "Context Usage Over Time" section to partials/stage_detail.html with inline SVG bar chart (responsive viewBox, tooltips per bar, session labels S1/S2/...). CSS for context-chart/chart-bar/chart-axis-label/chart-threshold-label styles. 14 new tests (7 integration: chart shown/hidden/threshold line/bar colors/custom threshold/null session skip/tooltips, 6 unit: _build_context_chart returns None/bar count/threshold default/custom/colors/grid lines, 1 PBT: bar count matches non-null session count). 1160 total tests

## Phase 13: Spec Completeness — HTN Task Detail

- [x] 48. Show precondition/postcondition status on HTN task nodes — spec line 958 requires "Each task shows: name, assigned session, checkpoint rev, precondition/postcondition status". Added _parse_conditions() helper in routes/pipelines.py (defensive JSON parsing with type+description extraction, fallback to type when description missing, skips non-dict entries). Extended _build_task_tree() to parse preconditions_json/postconditions_json into display-ready lists. Updated partials/htn_task_node.html with Preconditions/Postconditions sections: condition type badges (monospace), description text, status indicators (checkmark for completed/in_progress preconditions, checkmark for completed postconditions, X for failed, circle for pending). CSS for task-conditions/conditions-list/condition-item/condition-type-badge/condition-status with passed(green)/failed(red)/pending(gray) colors. 14 new tests (7 integration: preconditions shown, postconditions shown, completed=passed, ready=pending, failed=failed, no conditions=no section, pipeline detail page conditions; 5 unit: _parse_conditions valid/empty/invalid/fallback/non-dict; 1 PBT: conditions roundtrip preserves type+description; 1 integration: multiple conditions all rendered). 1174 total tests

## Phase 14: HTN Task Color Correctness

- [x] 49. Add pink "needs human" coloring for decision-type HTN tasks + fix CSS class hyphenation — spec line 958 requires "pink=needs human" in the color-coded task tree. Added decision-type override in _build_task_tree(): decision tasks with status != completed get task-decision class (pink #e91e63 border). Fixed CSS class name mismatch: _TASK_STATUS_CLASS was generating underscored names (task-in_progress, task-not_ready) but CSS selectors used hyphens (task-in-progress, task-not-ready) — in-progress and not-ready task colors were silently broken. Updated PBT test_htn_task_status_renders_with_class to use hyphenated expected classes. 12 new tests (4 integration: pink class on pipeline detail and tasks page for ready/blocked decision tasks, completed decision uses green, in-progress hyphenated; 4 unit: _build_task_tree decision override, completed not overridden, primitive not overridden, hyphenated classes; 3 integration: not-ready hyphenated, decision completed no pink on tasks page, in-progress on tasks page; 1 PBT: decision tasks always pink for any non-completed status). 1186 total tests

## Phase 15: Clone Cleanup DB Marking

- [x] 50. Mark pipeline as "cleaned" in DB after clone cleanup — spec line 965 requires "marks pipeline as cleaned". Previously cleanup_pipeline_clone() and cleanup_completed_clones() only deleted the clone directory without updating the database. Added clone_cleaned_at TIMESTAMPTZ column to pipelines schema, changed clone_path from NOT NULL to nullable. After shutil.rmtree, both per-pipeline and bulk cleanup handlers now UPDATE pipelines SET clone_path=NULL, clone_cleaned_at=now(). API cleanup route (routes/api.py) also updated. Dashboard terminal_count excludes already-cleaned pipelines (clone_path IS NOT NULL check). Pipeline card shows "Clone cleaned" badge instead of cleanup button when already cleaned. Pipeline detail page shows cleaned state with timestamp, hides clone path/copy button/size/cleanup button. CSS for .clone-cleaned/.clone-cleaned-badge/.clone-cleaned-at. _get_clone_size handles None clone_path. 9 new tests (5 integration: DB state after cleanup with clone_path=NULL and clone_cleaned_at set, idempotent second cleanup, bulk cleanup marks all, detail page cleaned state, detail page hides copy path; 2 dashboard: terminal count excludes cleaned, card shows cleaned badge; 1 PBT: any terminal status marks DB; 1 unit: bulk cleanup DB update). 1195 total tests

## Phase 16: Pipeline Lifecycle Completeness

- [x] 51. Wire CloneManager into orchestrator pipeline lifecycle — spec lines 496-498 require "Clone the repo to an isolated directory" then "Capture review_base_rev" before "Acquire a pipeline lease". Previously _run_pipeline skipped directly to lease acquisition, leaving clone_path="" for all stage runners. Added CloneManager as injectable dependency on PipelineOrchestrator (same pattern as LeaseManager/RecoveryManager), added _ensure_clone() method (checks clone_path+directory existence, calls create_clone() for fresh pipelines, handles missing-directory re-clone for deleted clones, skips for resumed pipelines), wired _ensure_clone as first step in _run_pipeline before lease acquisition. Updated test seed helpers (_seed_pipeline, _seed_full_pipeline) to create clone directories so _ensure_clone skips in tests. 9 new tests (7 unit: skip when exists, clone when empty, reclone when missing, not-found error, injectable, default created, call order before lease; 1 integration: fresh pipeline e2e with mock create_clone side-effect updating DB; 1 PBT: idempotent ensure_clone). 1204 total tests

## Phase 17: Pipeline Authoring & Inspection UX

- [x] 52. Pipeline definition detail page + preview, repo folder picker, and new-pipeline UX polish — spec line 982 (pipeline builder) only listed pipeline definitions but never let the user inspect what one will do, the repo creation form required typing absolute paths from memory, and the new-pipeline form had no signal about what a chosen definition would do or that a freshly-created pipeline was still initializing. Added GET /pipeline-defs/{id} (full detail page with stage-graph viz, per-node config, review sub-config, edges table, entry badge) and GET /pipeline-defs/{id}/preview (HTMX HTML fragment summarizing nodes/edges/entry stage), wired into pipeline_def_detail.html template. Added GET /repos/browse?path= as an HTMX-driven directory listing fragment (filters dotfiles, parent navigation, double-click select, permission/error messaging) and integrated into new_repo.html with a Browse button + folder picker drawer. Pipeline creation form (new_pipeline.html) now: pre-selects ?repo_id=N when arriving from a repo card, shows a field-hint under the def selector, and HTMX-loads the def preview fragment into #def-preview when the user changes the selector. Pipeline detail page now renders a starting-up banner for status='pending' explaining the orchestrator is cloning the repo (with cancel button inline), so users do not stare at an empty page. Pipeline def list cards became <a href="/pipeline-defs/{id}"> links (.builder-def-link). CSS additions for .browse-* (folder picker), .def-preview-* (def summary fragment), .def-detail-* / .def-node-* / .def-edges-table (detail page), .pipeline-pending-banner, .field-hint, .builder-def-link. Also added docs/reference/development-workflow-ontology.md (taxonomy of LLM-assisted dev workflows derived from session analysis). 35 new tests (12 detail page incl. 1 PBT + 5 preview endpoint + 9 folder browser + 1 new-repo browse button + 3 new-pipeline UX + 3 pending banner + 2 list link). 1239 total tests, 0 warnings
