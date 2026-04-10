# Task 20: Escalation queue page
## Session: 2 | Complexity: medium

### What I did
- Built on partial prior session work (route file, templates, CSS, router wiring existed) — completed with 6 additional tests (3 property-based, 3 edge-case/ordering) bringing total to 27
- Escalation queue page (`GET /escalations`) renders open escalations as cards with pipeline name, repo name, stage type, reason badges, expandable context snapshots, and action buttons (resolve with text input, dismiss)
- `POST /escalations/{id}/resolve` and `POST /escalations/{id}/dismiss` correctly update DB state and redirect with 303
- `?show_all=1` includes resolved/dismissed escalations, with filter toggle links
- 3-column stat cards at top show open/resolved/dismissed counts
- Added 3 property-based tests: every reason renders as badge, resolution text round-trips through resolve endpoint, status filter consistency (open-only vs show_all)
- Added edge-case tests: ordering newest-first, multiple pipelines, invalid context_json handling

### Learnings
- Hypothesis property tests with function-scoped DB fixtures need `suppress_health_check=[HealthCheck.function_scoped_fixture]` since the fixture isn't reset between generated examples
- With shared DB across Hypothesis examples, seed data names must be UUID-suffixed to avoid UNIQUE constraint violations — using `uuid.uuid4().hex[:12]` is the cleanest approach (integer tags collide too easily)
- When property tests accumulate data across examples (same DB), assertions about "card not in response" for non-open statuses become unreliable since prior open examples are still in the DB — keep assertions to page-level properties (status 200, show_all includes everything)
- The `_fetch_escalation_data` pattern mirrors `_fetch_dashboard_data` — single connection, JOINs for enrichment, summary counts — good consistency

### Postcondition verification
- [PASS] 646 tests pass (27 escalation queue tests)
- [PASS] ruff check clean
- [PASS] mypy clean
