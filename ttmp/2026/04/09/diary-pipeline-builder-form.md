# Diary: Pipeline Builder Form — stage-graph definition UI

## Task
Phase 4, Task 22: Pipeline builder form with explicit node/edge editing.

## What I did
- Created `routes/pipeline_defs.py` with three routes:
  - `GET /pipeline-defs` — list existing definitions with enriched node/edge counts
  - `GET /pipeline-defs/new` — builder form (same template, no list data)
  - `POST /pipeline-defs` — create pipeline definition with full validation
- Form parsing via `_parse_nodes_from_form()` and `_parse_edges_from_form()` — extract indexed form fields (node_0_key, edge_1_from, etc.) into structured dicts
- `_build_stage_graph_json()` assembles and validates via `StageGraph.from_json()` before DB insert
- Created `templates/pipeline_builder.html`:
  - Lists existing pipeline defs at top
  - Builder form with dynamic node/edge sections using `<template>` elements and vanilla JS cloning
  - Each node: key, name, stage type, agent, prompt (with datalist autocomplete), model, max iterations, context threshold, optional review config (collapsible details), optional devbrowser/record checkboxes
  - Each edge: key, from, to, guard condition, optional max_visits, on_exhausted
  - JS handles add/remove of node and edge rows with index-based naming
- Added CSS styles for builder form components
- Wired router in `main.py`, added "Pipeline Defs" to nav

## Key decisions
- Used indexed form fields (node_0_key) rather than JSON textarea — more accessible, progressive enhancement friendly
- Used HTML `<template>` + JS `cloneNode` for dynamic rows — minimal JS, no framework dependency
- Combined list and builder on same page per spec's single `pipeline_builder.html` template
- Validation errors return 422 with error message in template; form data is not preserved (acceptable for v1, spec says "start simple")
- Review sub-config is optional and hidden behind `<details>` — only parsed when review_agent is non-empty

## Learnings
- `StageGraph.from_json()` already does thorough validation (duplicate keys, missing entries, bad edge refs) — no need to duplicate those checks in the route layer, just catch `StageGraphError`
- Form `<template>` elements with IDX placeholder replacement is a clean pattern for dynamic form rows without heavy JS
- psycopg unique constraint violations contain "duplicate" in the error string, making the error detection straightforward
- The existing test pattern (ASGI transport + seeding helpers + follow_redirects=False for POST/redirect) works cleanly for form submission tests

## Stats
- 36 new tests (6 list/form rendering + 7 happy-path creation + 7 validation errors + 1 optional fields + 8 node parsing + 6 edge parsing + 3 property-based)
- 727 total tests passing
