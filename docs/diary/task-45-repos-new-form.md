# Task 45: Add GET /repos/new route and template

## Session | Complexity: trivial | Tests: 1145

### What I did
- Added `GET /repos/new` route handler (`new_repo_form()`) in `routes/repos.py`
- Created `new_repo.html` template with name, local_path, git_url, and default_branch fields
- Extended CSS to style text inputs with the same pattern as select elements in `.new-pipeline-form`
- Fixed a latent 404: repos.html already linked to `/repos/new` at two places (Add Repo button, empty-state prompt) but the route didn't exist

### Learnings
- The spec's API surface section (lines 1122-1173) is the authoritative route list; the implementation plan phases 1-5 (tasks 1-29) predate several Phase 6+ routes, so spec route coverage requires a manual gap analysis against the API surface table
- The repos router uses `APIRouter(prefix="/repos")` while the pipelines router is unprefixed (routes like `/pipelines/new` are full paths); this means the repos `/new` path is just `"/new"` in the route decorator
- Reusing `.new-pipeline-form` CSS class for the repo form is fine since it's a generic form layout (max-width, label spacing) — the class name is misleading but adding a second identical class would be worse
- The `box-sizing: border-box` on text inputs prevents them from overflowing their container when `width: 100%` is set alongside padding

### Postcondition verification
- [PASS] Route exists: GET /repos/new returns 200
- [PASS] Template renders with all 4 form fields
- [PASS] Form posts to /repos (existing handler)
- [PASS] All 1145 tests pass, ruff clean, mypy clean
