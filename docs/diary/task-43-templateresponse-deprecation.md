# Task 43: Fix Starlette TemplateResponse Deprecation Warnings
## Session: 1 | Complexity: small | Phase: 9

### What I did
- Migrated all 22 TemplateResponse calls across 6 route files from the deprecated
  `TemplateResponse(name, {"request": request, ...})` signature to the new
  `TemplateResponse(request, name, context={...})` API
- Refactored `_prompt_context()` in prompts.py and `_builder_context()` in
  pipeline_defs.py to remove `request` from the returned context dict (it's now
  passed as the first positional arg to TemplateResponse)
- Fixed a pre-existing Hypothesis flaky test: `test_diff_artifact_write_roundtrip`
  in test_code_review.py failed on surrogate characters (\ud800) during file I/O;
  added `blacklist_categories=("Cs",)` to the text strategy

### Learnings
- Starlette deprecated the old TemplateResponse API where `name` was the first
  parameter and `request` was embedded in the context dict. The new API takes
  `request` as the first positional parameter, followed by the template name,
  then an optional context dict that no longer needs `request`
- Helper functions that build template context dicts should not include `request` —
  it's now a separate concern passed directly to TemplateResponse
- The Hypothesis `Cs` (surrogate) Unicode category is the root cause of all the
  file I/O roundtrip test failures; blacklisting it at the strategy level is the
  clean fix rather than wrapping write_text in try/except

### Metrics
- Warnings reduced from 296 to 4 (only asyncio subprocess cleanup warnings remain)
- 6 files changed, 22 TemplateResponse calls migrated
- 1 flaky Hypothesis test fixed
- 1139 tests passing
