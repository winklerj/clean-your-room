# Task 46: Fix duplicate /repos/new route shadowing
## Session: 1 | Complexity: small | Phase: 11

### What I did
- Removed stale `GET /repos/new` route from `dashboard.py` that returned `add_repo.html`
- Deleted orphaned `add_repo.html` template (replaced by `new_repo.html` in Task 45)
- Added regression test asserting the styled template is served (CSS classes unique to `new_repo.html`)

### Learnings
- FastAPI uses first-match routing when multiple routers register the same path. The `include_router()` call order in `main.py` determines priority: dashboard_router (line 69) was included before repos_router (line 74), so the stale dashboard route won
- The existing tests for `/repos/new` all passed despite serving the wrong template because both templates shared the same form field names and `href="/repos"` appeared in the base nav template
- To prevent this class of bug, tests should assert on CSS classes or structural elements unique to the intended template, not just content that could come from shared base templates
- When adding new routes that overlap with existing ones, always grep for the path across all route files to check for conflicts

### Postcondition verification
- [PASS] Stale route removed from dashboard.py
- [PASS] add_repo.html template deleted
- [PASS] 1146 tests passing, 0 warnings
- [PASS] Lint clean, type check clean
