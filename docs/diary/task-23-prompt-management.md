# Task 23 — Prompt Management (Extend Existing)

## What was built

Extended the existing basic prompt CRUD at `GET/POST /prompts` into a production-ready prompt management page with:

1. **Usage tracking** (`_fetch_prompt_usage`): Scans all `pipeline_defs.stage_graph_json` for prompt name references in `node.prompt`, `node.fix_prompt`, and `node.review.prompt` fields. Maps prompt names → pipeline def names. Displayed as "N defs" with tooltip listing the def names.

2. **Delete protection**: Before deleting, checks usage map. Returns 409 with error message listing which pipeline defs reference the prompt. Covers all three prompt reference types (main, fix, review).

3. **Duplicate name error handling**: Both create (POST) and update (PUT) catch UNIQUE constraint violations and return 422 with a clear error message instead of 500.

4. **Filtering**: `GET /prompts?stage_type=X&agent_type=Y` query params with dropdown selectors. Parameterized SQL with WHERE clauses. Clear-filter link when active.

5. **Clone/duplicate**: `POST /prompts/{id}/clone` copies a prompt with `_copy` suffix, incrementing to `_copy_2`, `_copy_3` etc. if the name already exists.

6. **Template variable extraction**: `extract_template_variables()` uses regex to find `{{variable}}` patterns in prompt body. Displayed as purple badges below the prompt name.

7. **Full body view**: `<details>/<summary>` HTML for expand/collapse. Shows 80-char preview in collapsed state, full body in a scrollable pre block when expanded.

8. **Improved UI**: Stat cards (total, by-agent counts), builder-style form layout for create/edit, stage type and agent type as styled badges, consistent styling with pipeline builder and escalation pages.

## Key decisions

- **Usage tracking via JSON scanning**: Rather than adding a foreign key from pipeline_defs to prompts (which would require schema changes), usage is determined at render time by scanning `stage_graph_json`. This is fine because: (a) the number of pipeline defs is small (tens, not thousands), and (b) it catches all three reference types without needing to maintain a separate mapping table.

- **409 for in-use delete, not 422**: 409 Conflict is the correct HTTP status for "I understand what you want but the current state prevents it." This distinguishes from 422 (validation error on the input itself).

- **Enrichment pattern**: `_enrich_prompts()` adds `used_by` and `variables` fields to the raw DB rows. This keeps the DB query simple and adds computed fields in Python.

## Learnings

- The `psycopg` dict_row factory returns `dict[str, Any]` which is `None`-able from `fetchone()`. Need `# type: ignore[dict-item]` when unpacking with `**`.
- Jinja2 `{{ }}` in template text needs `&#123;&#123;` HTML entities to avoid being interpreted as Jinja expressions.
- HTMX `hx-swap="beforeend"` on clone appends the new row to the table body — works well for adding clones without a page refresh.

## Test coverage

31 tests (was 4):
- 3 page rendering (200 status, agent badges, stage badges)
- 4 filter tests (by stage, by agent, combined, no results)
- 6 CRUD tests (create, create duplicate 422, update, update duplicate 422, delete, delete nonexistent)
- 3 delete protection (main prompt, fix_prompt, review.prompt)
- 3 clone tests (basic, increments suffix, 404 nonexistent)
- 2 usage tracking (shows def count, shows unused)
- 2 edit/row form tests
- 5 variable extraction unit tests
- 3 property-based tests (Hypothesis: never raises, roundtrip, stage types valid)

Total: 754 tests (was 727).
