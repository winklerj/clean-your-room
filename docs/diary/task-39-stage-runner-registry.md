# Task 39: StageRunner Protocol and registry-based dispatch
## Session: 1 | Complexity: medium | Files: 9 changed, 1 new

### What I did
- Created `stages/base.py` with `StageRunnerFn` type alias, `STAGE_RUNNERS` registry, `register_stage_runner()` (with conflict detection), and `get_stage_runner()` lookup
- Added module-level self-registration calls at the end of all 5 stage runner modules
- Updated `stages/__init__.py` to re-export registry API and import concrete modules to trigger registration
- Refactored orchestrator from 5-branch if/elif dispatch chain to single `get_stage_runner()` registry lookup
- Updated 14 integration test patch blocks from `patch("orchestrator.run_X_stage")` to `patch.dict("stages.base.STAGE_RUNNERS")`
- Wrote 17 new tests: registry population, identity checks for all 5 runners, lookup behavior, conflict detection, idempotency, orchestrator dispatch via DB, unknown stage fallback, 3 property-based

### Learnings
- `Callable[..., Awaitable[str]]` is pragmatic for registry types when concrete callables differ in optional kwargs; a strict Protocol would reject runners with extra optional params
- `patch.dict()` is the correct way to mock a module-level registry dict, much cleaner than 5 separate `patch()` calls — and restores automatically on context exit
- Self-registration at module bottom with a late import (`from .base import register_stage_runner  # noqa: E402`) avoids circular import issues since base.py has no deps on concrete runners
- The `stages/__init__.py` import chain ensures registration happens when the package is first imported, which the orchestrator triggers via `from build_your_room.stages import get_stage_runner`
- `StageGraph.from_json()` expects a dict, not a JSON string — caught during test writing

### Open Questions
- None — all spec-mandated structure now matches
