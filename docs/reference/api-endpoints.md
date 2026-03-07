# API Endpoints

All endpoints are served by the FastAPI application. Responses are HTML (Jinja2-rendered templates) unless otherwise noted. Endpoints that accept form data use `application/x-www-form-urlencoded` encoding.

---

## Dashboard

### `GET /`

Returns the main dashboard page listing all active repositories with their most recent job status.

**Response:** Renders `dashboard.html`.

### `GET /repos/new`

Returns the form page for adding a new repository.

**Response:** Renders `add_repo.html`.

---

## Prompts (`/prompts`)

### `GET /prompts`

Lists all prompts.

**Response:** Renders `prompts.html`.

### `POST /prompts`

Creates a new prompt.

| Form Parameter | Type   | Required | Description              |
|----------------|--------|----------|--------------------------|
| `name`         | string | Yes      | Display name for prompt  |
| `template`     | string | Yes      | Prompt template content  |

**Response:** Returns `prompt_row.html` partial (for HTMX insertion).

### `PUT /prompts/{prompt_id}`

Updates an existing prompt.

| Path Parameter | Type    | Description       |
|----------------|---------|-------------------|
| `prompt_id`    | integer | ID of the prompt  |

| Form Parameter | Type   | Required | Description              |
|----------------|--------|----------|--------------------------|
| `name`         | string | Yes      | Updated name             |
| `template`     | string | Yes      | Updated template content |

**Response:** Returns `prompt_row.html` partial.

### `DELETE /prompts/{prompt_id}`

Deletes a prompt.

| Path Parameter | Type    | Description       |
|----------------|---------|-------------------|
| `prompt_id`    | integer | ID of the prompt  |

**Response:** Empty response (HTTP 200).

### `GET /prompts/{prompt_id}/edit`

Returns the inline edit form for a prompt.

| Path Parameter | Type    | Description       |
|----------------|---------|-------------------|
| `prompt_id`    | integer | ID of the prompt  |

**Response:** Returns `prompt_form.html` partial.

### `GET /prompts/{prompt_id}/row`

Returns the display row for a prompt (used to cancel an edit).

| Path Parameter | Type    | Description       |
|----------------|---------|-------------------|
| `prompt_id`    | integer | ID of the prompt  |

**Response:** Returns `prompt_row.html` partial.

---

## Repos (`/repos`)

### `POST /repos`

Adds a new repository. Parses the GitHub URL to extract the org and repo name, stores the record in the database, and clones the repository to the local filesystem.

| Form Parameter | Type   | Required | Description                                      |
|----------------|--------|----------|--------------------------------------------------|
| `github_url`   | string | Yes      | Full GitHub URL (e.g. `https://github.com/org/repo`) |

**Response:** HTTP 303 redirect to `GET /repos/{repo_id}`.

### `GET /repos/{repo_id}`

Returns the detail page for a single repository, including its job history and a prompt selection form for starting new jobs.

| Path Parameter | Type    | Description          |
|----------------|---------|----------------------|
| `repo_id`      | integer | ID of the repository |

**Response:** Renders `repo_detail.html`.

### `POST /repos/{repo_id}/archive`

Archives a repository by setting its status to `archived`.

| Path Parameter | Type    | Description          |
|----------------|---------|----------------------|
| `repo_id`      | integer | ID of the repository |

**Response:** HTTP 303 redirect to `GET /repos/{repo_id}`.

---

## Jobs (`/jobs`)

### `POST /jobs`

Creates a new job and starts it as a background task.

| Form Parameter        | Type    | Required | Default | Description                              |
|-----------------------|---------|----------|---------|------------------------------------------|
| `repo_id`             | integer | Yes      | —       | ID of the target repository              |
| `prompt_id`           | integer | Yes      | —       | ID of the prompt to use                  |
| `feature_description` | string  | No       | `""`    | Optional description of the feature      |
| `max_iterations`      | integer | No       | `20`    | Maximum agent iterations before stopping |

**Response:** HTTP 303 redirect to `GET /jobs/{job_id}`.

### `GET /jobs/{job_id}`

Returns the job viewer page showing job details, associated repo information, prompt name, and logs.

| Path Parameter | Type    | Description    |
|----------------|---------|----------------|
| `job_id`       | integer | ID of the job  |

**Response:** Renders `job_viewer.html`.

### `POST /jobs/{job_id}/stop`

Stops a running job by setting its cancel event.

| Path Parameter | Type    | Description    |
|----------------|---------|----------------|
| `job_id`       | integer | ID of the job  |

**Response:** HTTP 303 redirect to `GET /jobs/{job_id}`.

### `POST /jobs/{job_id}/restart`

Creates a new job using the same parameters (repo, prompt, feature description, max iterations) as the specified job.

| Path Parameter | Type    | Description               |
|----------------|---------|---------------------------|
| `job_id`       | integer | ID of the job to restart  |

**Response:** HTTP 303 redirect to `GET /jobs/{new_job_id}`.

### `GET /jobs/{job_id}/stream`

Server-Sent Events (SSE) endpoint for real-time log streaming. The client maintains an open connection and receives log entries as they are produced by the job runner.

| Path Parameter | Type    | Description    |
|----------------|---------|----------------|
| `job_id`       | integer | ID of the job  |

**Response:** `text/event-stream` via `EventSourceResponse`.
