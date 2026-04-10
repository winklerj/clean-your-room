# Task 20: Escalation queue page
## Session: 1 | Complexity: medium

### What I did
- Registered the existing `escalations_router` in `main.py` (route file, templates, and CSS were already created in a prior session but never wired up)
- Verified the escalation queue page (`GET /escalations`) renders open escalations as cards with pipeline name, repo name, stage type, reason badges, expandable context snapshots, and action buttons (resolve with text input, dismiss)
- Verified `POST /escalations/{id}/resolve` and `POST /escalations/{id}/dismiss` correctly update DB state and redirect with 303
- Verified `?show_all=1` includes resolved/dismissed escalations, with filter toggle links
- Stat cards at the top show open/resolved/dismissed counts
- Wrote 21 tests covering: empty queue, card rendering, reason badges, context snapshots, action buttons, resolved exclusion, show_all mode, multiple escalations, stat counts, null stage handling, pipeline status display, filter toggle, resolve action + redirect + idempotency guard, dismiss action + redirect + idempotency guard, resolution text display, no action buttons on resolved, pipeline link

### Learnings
- The route, template, partial, and CSS had all been created in a prior session that was interrupted before wiring and testing — the only missing pieces were the main.py router registration and tests
- The escalation route uses `APIRouter()` without a prefix and defines paths like `/escalations` directly, unlike the prompts router which uses `prefix="/prompts"` — both patterns work but consistency would be nice in future
- The resolve/dismiss endpoints use `WHERE status = 'open'` as an idempotency guard, so re-resolving a resolved escalation is safely a no-op
- The `RedirectResponse(url="/escalations", status_code=303)` pattern is the correct PRG (Post/Redirect/Get) approach for form submissions to prevent double-submit on refresh

### Postcondition verification
- [PASS] 640 tests pass (21 new escalation tests)
- [PASS] ruff check clean
- [PASS] mypy clean
