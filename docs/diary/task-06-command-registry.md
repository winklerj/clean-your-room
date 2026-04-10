# Task 6: Command-template registry for repo-standard verification commands
## Session | Complexity: medium | Tests: 55 new (191 total)

### What I did
- Created `src/build_your_room/command_registry.py` with:
  - `ConditionResult` frozen dataclass for postcondition/precondition check results
  - `CommandTemplate` frozen dataclass with `base_args`, `suffix_args`, and `build_args()` for predictable command construction
  - `CommandRegistry` class with default templates for `tests_pass` (uv run pytest -v), `lint_clean` (uv run ruff check), and `type_check` (uv run mypy)
  - `run_cmd` async subprocess runner with `shell=False`, scrubbed env (only PATH/HOME/etc forwarded), optional `allowed_roots` path guard
  - `expand_test_targets` and `expand_paths` helpers for condition dict → command arg expansion
  - `VerifierRegistry` class for custom app-authored verifier functions dispatched by ID
  - Built-in `python_symbol_exists` custom verifier
  - `verify_condition` async dispatcher handling all 6 condition types: file_exists, tests_pass, lint_clean, type_check, task_completed, custom_verifier
- Created `tests/test_command_registry.py` with 55 tests including property-based tests for template construction and path expansion

### Learnings
- The `_scrubbed_env()` pattern is important for SideEffectsContained: verifier subprocesses should not inherit secrets, API keys, or other sensitive env vars from the host process
- `CommandTemplate.build_args()` with base/extra/suffix ordering keeps command construction predictable and testable — the caller only provides the dynamic middle portion
- `task_completed` condition type delegates DB lookup to the caller via a callback (`task_status_lookup`), keeping the command registry independent of the database layer — clean separation for the HTNPlanner (Task 9) to provide the callback
- The `verify_condition` dispatcher is designed to be the single entry point for both precondition and postcondition checking, used by the HTNPlanner's `verify_postconditions()` method

### Postcondition verification
- [PASS] ruff check: all files lint clean
- [PASS] mypy: no type errors
- [PASS] pytest: 191 tests pass (55 new)
