# Task 47: Context Usage Chart Over Time

## What I did
- Added a "Context Usage Over Time" inline SVG bar chart to the stage detail partial
- Each bar represents a session with non-null `context_usage_pct`, color-coded by thresholds (green <50%, yellow 50-80%, red >80%)
- Dashed threshold line from the pipeline's `config_json.context_threshold_pct` (default 60%)
- Grid lines at 0/25/50/75/100% with Y-axis labels
- Session labels (S1, S2, ...) on X-axis; tooltips on each bar

## Design decisions
- **Pre-computed SVG coordinates in Python** rather than Jinja template math: Jinja2 arithmetic is fragile for floats, and moving all coordinate computation to `_build_context_chart()` makes the template purely declarative and the logic independently testable with unit tests
- **Sessions with null context_usage_pct are excluded** from the chart but still count for session index numbering (S1, S3 if S2 is null), so the labels match the session list below
- **Threshold from pipeline config_json**: queried with a simple additional SELECT in `_fetch_stage_detail()`, falls back to 60% on parse errors or missing config
- **Inline SVG** over a charting library: zero dependencies, server-rendered, works with HTMX partial loading, responsive via viewBox

## Learnings
- `_build_context_chart` returning `None` when no data exists makes the template conditional clean (`{% if context_chart %}`)
- Using `max(bar_h, 0)` prevents negative SVG rect heights when `context_usage_pct` is 0
- The existing `progress-ctx-ok/warn/high` CSS colors map directly to `chart-bar-ok/warn/high` fills, keeping the visual language consistent
- Testing SVG content in HTML responses works well with substring checks on class names and text content; counting `class="chart-bar "` occurrences verifies bar count

## Postcondition verification
- [PASS] ruff check: all clean
- [PASS] mypy: 0 errors
- [PASS] pytest: 1160 tests, 0 warnings
