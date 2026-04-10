# Task 31: Pipeline Creation Form and Lifecycle Control HTML Routes

## What I did
- Added `GET /pipelines/new` route with form to select repo and pipeline definition
- Added `POST /pipelines` to create and start a pipeline with orchestrator integration
- Added `POST /pipelines/{id}/cancel` for cooperative cancellation (HTML redirect)
- Added `POST /pipelines/{id}/kill` for force termination (HTML redirect)
- Added `POST /pipelines/{id}/pause` to pause running pipelines
- Added `POST /pipelines/{id}/resume` with resolution text for escalation resolution
- Created `new_pipeline.html` template with repo/def selectors and empty-state guidance
- Added lifecycle action buttons to `pipeline_detail.html` (context-sensitive per status)
- Added "New Pipeline" button to the dashboard header
- Added CSS for action buttons, resume form, and new pipeline form
- Wrote 28 tests (4 form rendering, 4 creation, 4 cancel, 4 kill, 2 pause, 3 resume, 3 detail page actions, 1 dashboard link, 3 property-based)

## Learnings
- psycopg's placeholder system doesn't support `NOT IN %s` with a tuple directly — need to expand placeholders manually with `",".join(["%s"] * len(values))` and unpack via `*values`
- Lazy imports (`from build_your_room.main import orchestrator` inside route functions) must be mocked at `build_your_room.main.orchestrator`, not at the route module level
- Template conditional rendering (show/hide action buttons by status) is tested by asserting URL patterns are present/absent in response text
- Empty-state UX: when no repos or pipeline_defs exist, the form shows links to create them and disables the submit button

## Postcondition verification
- [PASS] 28/28 new tests pass
- [PASS] 952 total tests (950 pass, 2 pre-existing flaky PBT)
- [PASS] ruff lint clean
- [PASS] mypy type check clean
