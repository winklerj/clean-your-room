# Task 25: Property-Based Tests for Orchestrator, Stage Graph, and HTN Claims

## What was done

Added 26 new Hypothesis `@given` property-based tests across three test files, covering the three core state-machine areas:

### Stage Graph (10 tests in `test_stage_graph.py`)
- Custom `valid_stage_graphs()` composite strategy generates random DAGs with 2-6 nodes, chain edges, optional back-edges with max_visits
- Invariants tested: entry_stage validity, edge reference integrity, key uniqueness, outgoing edge filtering, resolve matching/unmatched guards, visit count gating on bounded edges, traversal monotonicity

### Orchestrator (8 tests in `test_orchestrator.py`)
- Visit count JSON roundtrip (serialize → deserialize is identity)
- Graceful handling of invalid/null recovery_state_json
- Default stage result always returns a valid string for all stage types
- Lease acquire/release/reacquire cycles always succeed
- Lease exclusivity: second acquire on same pipeline always fails
- Reconciliation downgrades all N expired pipelines
- Completed pipeline has exactly one stage row per graph node

### HTN Claims (8 tests in `test_htn_planner.py`)
- Custom `independent_primitive_tasks()` and `chain_primitive_tasks()` strategies
- Claim sets all ownership fields, complete clears them
- Priority ordering: highest-priority always selected first
- N sequential claims on N ready tasks yield N distinct tasks
- Release → reclaim cycle works
- Chain readiness propagation: each completion unlocks exactly the next task
- Fail blocks only hard dependents
- Compound parent auto-completes when all generated children complete

## Bug found and fixed

The `_load_visit_counts` method in `orchestrator.py` crashed when `recovery_state_json` was the string `"null"` — `json.loads("null")` returns Python `None`, and calling `.get()` on `None` raised `AttributeError`. Fixed by adding an `isinstance(data, dict)` guard before the `.get()` call. This is exactly the kind of edge case property-based testing excels at finding.

## Learnings

1. **Property tests find real bugs**: The `"null"` JSON edge case in visit counts was immediately caught by the Hypothesis test for graceful invalid JSON handling. Unit tests with hand-picked examples wouldn't have caught this.

2. **Strategy design matters for DB-backed property tests**: DB-backed Hypothesis tests need `uuid.uuid4().hex[:8]` suffixes on seeded names to avoid UNIQUE constraint violations across Hypothesis examples. The `suppress_health_check=[HealthCheck.function_scoped_fixture]` setting is required for DB fixtures.

3. **Edge ordering matters in graph resolution**: `resolve_next_stage` returns the *first* matching edge. When testing bounded edges with `max_visits`, the test must verify the bounded edge is actually the first match for its guard from that node — otherwise another unbounded edge with the same guard may match first.

4. **Composite strategies vs parametrize**: For DB-backed tests, composite strategies that generate entire task graphs are much more powerful than parametrize — they explore combinations of parent/child relationships, dependency chains, and priority orderings that would be tedious to enumerate manually.

## Test count

805 total (was 779, +26 new property-based tests)
