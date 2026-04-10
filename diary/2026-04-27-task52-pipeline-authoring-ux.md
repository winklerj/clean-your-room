# Task 52: Pipeline definition detail page + folder picker + new-pipeline UX polish
## Session: 1 | Complexity: medium

### What I did
- Added GET /pipeline-defs/{id} (full detail page) and GET /pipeline-defs/{id}/preview
  (HTMX HTML fragment summarizing nodes/edges/entry stage), backed by
  `_fetch_pipeline_def_detail()` which parses `stage_graph_json` defensively.
- Wrote `pipeline_def_detail.html`: stage graph viz, per-node config grid (type,
  agent, model, prompt, max_iterations, context threshold, on_context_limit,
  fix_agent/prompt for code_review, devbrowser flags), nested review sub-config,
  edges table with max_visits/on_exhausted columns, entry-stage badge.
- Made the def list page cards (`pipeline_builder.html`) into anchor links to
  the new detail route via `.builder-def-link`.
- Added GET /repos/browse?path= as an HTMX directory listing fragment: filters
  dotfiles, supports parent navigation, surfaces permission and not-found errors
  inline as `.browse-error`, doublclick selection via `data-path`. Wired the
  Browse button + folder picker drawer into `new_repo.html`.
- Pipeline creation form polish: `?repo_id=N` query param now pre-selects the
  matching `<option selected>`, a `field-hint` under the def selector explains
  what definitions do, and the selector HTMX-loads the new preview fragment into
  `#def-preview` on change (with a small inline JS shim to rewrite `hx-get` per
  selection).
- Pipeline detail page now renders a `.pipeline-pending-banner` for
  `status='pending'` (with cancel button inline) so users don't stare at an
  empty page while the orchestrator clones the repo. Removed the duplicate
  cancel button from `.pipeline-actions` for the pending state.
- CSS: `.browse-*` (picker), `.def-preview-*` (fragment), `.def-detail-*` /
  `.def-node-*` / `.def-edges-table` (detail), `.pipeline-pending-banner`,
  `.field-hint`, `.builder-def-link`.
- Captured the workflow taxonomy in `docs/reference/development-workflow-ontology.md`
  (459 sessions, 50+ projects analyzed) — this session is A1 spec-driven task queue.

### Learnings
- Returning HTML fragments from FastAPI is cleanest as `HTMLResponse(string)`
  with `response_class=HTMLResponse` on the route — no template required for
  small dynamic snippets like the def preview.
- For HTMX wiring on a `<select>` whose URL depends on the chosen value, we
  can't just put a Jinja `{{ }}` in `hx-get` (the URL has to change on every
  change event). The pattern that works: leave `hx-get=""` in the markup, then
  in JS rewrite the attribute on `change` and call `htmx.process(sel)` so HTMX
  re-binds. Lighter than building a real component.
- `Path.iterdir()` does not raise on dotfiles, but a `for x in iterdir(): if
  x.name.startswith('.'): continue` filter is enough to keep the listing clean
  without triggering hidden-folder permission errors on macOS.
- Returning HTTP 200 with an inline `.browse-error` (rather than a 4xx) keeps
  HTMX swap behavior simple: the user sees the error in the same panel without
  the request being treated as a network failure.
- Splitting a "show me a pending pipeline starting up" affordance into a
  dedicated banner (instead of relying on the tiny Cancel button in
  `.pipeline-actions`) is a cheap legibility win — users now know *why* they're
  staring at a sparse page during the clone phase.
- When `_seed_pipeline_def` already returns a single-stage def for tests, it's
  reusable for the entry-stage marker test even without elaborate fixtures —
  just check the fragment text contains `(entry)`.

### Postcondition verification
- [PASS] uv run ruff check src/ tests/ — All checks passed!
- [PASS] uv run mypy src/ --ignore-missing-imports — Success: no issues found
  in 39 source files
- [PASS] uv run pytest tests/ -q — 1239 passed in 63s, 0 warnings
- [PASS] uv run uvicorn build_your_room.main:app — server starts; smoke-checked
  GET / , /pipeline-defs , /pipeline-defs/{id} , /pipeline-defs/{id}/preview ,
  /repos , /repos/browse , /pipelines/new — all 200
- [PASS] HTMX preview: /pipeline-defs/7/preview returned `1 stage, 1 transition`
  HTML fragment as expected

### Open Questions
- The def preview fragment currently only summarizes structure. If usage demand
  arises, we could extend it to show recent run stats (success rate, average
  cost) per definition — but that's a separate analytics task.
- The folder browser walks any `Path(path).resolve()` the user types, including
  outside `$HOME`. That's intentional for now (the harness already enforces
  workspace sandboxing during execution), but a future hardening pass could
  restrict the browse root.
