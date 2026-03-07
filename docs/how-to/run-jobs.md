# How to run spec-generation jobs

## Start a job

1. Navigate to the repo detail page at `/repos/{id}`.
2. Select a **prompt** from the dropdown.
3. Optionally add a **feature description** to scope the spec.
4. Set **max iterations** (default: 20).
5. Submit the form.

The job starts immediately as a background task.

## Monitor a running job

After starting a job, you are redirected to the job viewer page. Output streams in real time via Server-Sent Events (SSE). Keep the page open to watch agent progress.

## Stop a running job

Send a POST request to halt execution:

```
POST /jobs/{id}/stop
```

On the job viewer page, click **Stop** to trigger this. The job status changes to `stopped`.

## Restart a job

Send a POST request to re-run a completed, stopped, or failed job:

```
POST /jobs/{id}/restart
```

This creates a new job with the same parameters (prompt, feature description, max iterations). The original job record is preserved.

## Job status lifecycle

```
pending --> running --> completed
                   \-> stopped
                   \-> failed
```

- **pending** -- job is queued
- **running** -- agent is actively generating the spec
- **completed** -- spec generation finished successfully
- **stopped** -- manually halted by user
- **failed** -- agent encountered an unrecoverable error

## Output handling

On successful completion, the generated spec is automatically copied to the specs-monorepo and committed.
