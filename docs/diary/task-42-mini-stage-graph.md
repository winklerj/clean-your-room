# Task 42: Stage-graph mini-visualization on pipeline cards

## What I did
- Added a stage-graph mini-visualization to each pipeline card on the dashboard
- Spec line 924 requires: "Current stage (highlighted in the stage-graph mini-visualization)"
- Extended `_fetch_dashboard_data()` in `routes/dashboard.py` to fetch `stage_graph_json` from `pipeline_defs`
- Added `_parse_mini_graph_nodes()` helper to extract ordered node key/name pairs from stage graph JSON
- Enriched each pipeline's data with a `mini_graph` list containing `is_active` flags based on `current_stage_key`
- Updated `partials/pipeline_card.html` to render nodes as small pills connected by arrows
- Added CSS classes: `stage-graph-mini`, `mini-node`, `mini-node-active`, `mini-arrow`
- Fixed 3 pre-existing Hypothesis property test edge cases discovered during full suite run

## Learnings
- The dashboard route already fetched `pipeline_defs.name` separately from the main pipelines query. Extending the same query to include `stage_graph_json` was the minimal change needed — no schema or join changes required.
- The `_parse_mini_graph_nodes` helper returns a simple list of `{key, name}` dicts rather than full `StageNode` objects, keeping the template data minimal and avoiding coupling dashboard code to the `StageGraph` class.
- Hypothesis `st.characters()` without `blacklist_categories=("Cs",)` can generate surrogate characters (`\ud800`-`\udfff`) that are valid Python strings but can't be encoded to UTF-8 for file I/O. This caused flaky property tests in `test_impl_plan.py` and `test_spec_author.py`.
- The `_parse_nodes_from_form` function in pipeline_defs strips whitespace from names and falls back to the key when empty. The property test for roundtrip needed to account for this by filtering out names where `s != s.strip()`.
- The mini-visualization uses a horizontal flexbox layout with `flex-wrap: wrap` so it handles both short (2-node) and long (5+ node) pipelines gracefully on different card widths.

## Postcondition verification
- [PASS] `uv run ruff check src/ tests/` — all checks passed
- [PASS] `uv run mypy src/ --ignore-missing-imports` — 0 errors
- [PASS] `uv run pytest tests/ -v` — 1139/1139 passed (8 new tests)
