# Task 41: Clone size indicator on pipeline detail page

## Session: 1 | Complexity: small

### What I did
- Added `_get_clone_size()` helper in `routes/pipelines.py` that walks a directory with `rglob("*")`, sums file sizes, and formats as B/KB/MB/GB
- Wired clone_size into `_fetch_pipeline_detail()` return dict
- Added clone-size div to `pipeline_detail.html` between clone path and revision info
- Added CSS rules for `.clone-size`, `.clone-size-label`, `.clone-size-value`
- Wrote 9 tests: 6 unit tests for the helper function, 2 integration tests for template rendering, 1 property-based test for formatting invariants

### Learnings
- `Path.rglob("*")` with `is_file()` is clean and handles symlinks gracefully since `stat()` follows symlinks by default
- The graceful None return for missing/empty paths keeps the template simple with a single `{% if clone_size %}` guard
- Pre-existing flaky Hypothesis tests (test_artifact_write_roundtrip, test_parse_nodes_roundtrip) continue to fail intermittently on whitespace edge cases, unrelated to this change

### Postcondition verification
- [PASS] ruff check: all clean
- [PASS] mypy: no issues
- [PASS] pytest: 1131 total (1129 passed, 2 pre-existing flaky)
