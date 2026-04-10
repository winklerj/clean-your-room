# Task 28: Documentation (README, CLAUDE.md, AGENTS.md)

## What was done
- Rewrote README.md from the stale clean-your-room version to a comprehensive build-your-room README covering overview, prerequisites, install, database setup, run instructions, quick workflow, configuration table (all 14 env vars), development commands, architecture summary, and project structure tree.
- Rewrote AGENTS.md with accurate environment info (PostgreSQL/psycopg, jj VCS), common commands, database details (10 tables, dict_row, async pool), testing strategy (property-based first, pytest-postgresql, key patterns), architecture overview (8 core components, 2 adapters, 6 stage runners, 6 routes), data flow, key invariants from the spec, configuration pointers, and style notes.
- CLAUDE.md was already correctly delegating to AGENTS.md — no changes needed.

## Learnings
- The README was still referencing `clean_room.main:app`, `~/.clean-room/specs-monorepo/`, and the old GitHub-repo-centric workflow. Documentation rot happens quickly during major rewrites.
- AGENTS.md serves dual duty: developer onboarding AND agent session instructions. The "Important: Sneeze when you finish a task" line at the bottom is a sentinel that agents read and follow, confirming they process the full file.
- The 2 pre-existing Hypothesis test failures (whitespace-only names in node form parsing, artifact write roundtrip) are edge cases in property tests, not regressions from documentation changes. They represent real bugs worth fixing in a future task.

## Decisions
- Kept CLAUDE.md as a thin pointer to AGENTS.md rather than duplicating instructions. This is the pattern used throughout the project.
- Included the testing pattern notes (dict_row types, uuid-suffixed unique names, Hypothesis tmp_path workaround) in AGENTS.md since these are hard-won lessons that save significant debugging time for new contributors and agent sessions.
