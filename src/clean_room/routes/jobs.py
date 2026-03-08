import asyncio
from pathlib import Path
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sse_starlette.sse import EventSourceResponse

from clean_room.config import DB_PATH, SPECS_MONOREPO_DIR
from clean_room.db import get_db
from clean_room.runner import JobRunner
from clean_room.streaming import LogBuffer
from clean_room.git_ops import pull_repo, commit_specs

router = APIRouter(prefix="/jobs")

log_buffer = LogBuffer()
active_jobs: dict[int, asyncio.Event] = {}
running_tasks: dict[int, asyncio.Task] = {}


async def _start_job(
    job_id: int, repo_path: Path, slug: str, prompt: str, max_iterations: int
):
    """Background task that runs the job."""
    db = await get_db(DB_PATH)
    specs_path = SPECS_MONOREPO_DIR / slug
    try:
        await db.execute(
            "UPDATE jobs SET status='running', started_at=datetime('now') WHERE id=?",
            (job_id,),
        )
        await db.commit()

        try:
            await pull_repo(repo_path)
        except Exception:
            pass

        cancel_event = active_jobs[job_id]
        runner = JobRunner(
            job_id=job_id,
            repo_path=repo_path,
            specs_path=specs_path,
            prompt=prompt,
            max_iterations=max_iterations,
            log_buffer=log_buffer,
            cancel_event=cancel_event,
        )
        await runner.run(db=db)

        status = "stopped" if cancel_event.is_set() else "completed"
        await db.execute(
            "UPDATE jobs SET status=?, completed_at=datetime('now') WHERE id=?",
            (status, job_id),
        )
        await db.commit()
        log_buffer.close(job_id)

        await commit_specs(
            SPECS_MONOREPO_DIR,
            f"{'Partial specs' if cancel_event.is_set() else 'Specs'} for {slug}",
        )
    except Exception:
        await db.execute(
            "UPDATE jobs SET status='failed', completed_at=datetime('now') WHERE id=?",
            (job_id,),
        )
        await db.commit()
        log_buffer.close(job_id)
    finally:
        await db.close()
        active_jobs.pop(job_id, None)
        running_tasks.pop(job_id, None)


@router.post("", response_class=RedirectResponse)
async def create_job(
    repo_id: int = Form(),
    prompt_id: int = Form(),
    feature_description: str = Form(""),
    max_iterations: int = Form(20),
):
    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute(
            "INSERT INTO jobs (repo_id, prompt_id, feature_description, max_iterations) "
            "VALUES (?, ?, ?, ?) RETURNING id",
            (repo_id, prompt_id, feature_description or None, max_iterations),
        )
        row = await cursor.fetchone()
        job_id = row[0]
        await db.commit()

        cursor = await db.execute("SELECT clone_path, slug FROM repos WHERE id=?", (repo_id,))
        repo_row = await cursor.fetchone()
        cursor = await db.execute("SELECT template FROM prompts WHERE id=?", (prompt_id,))
        prompt_row = await cursor.fetchone()

        repo_path = Path(repo_row[0])
        slug = repo_row[1]
        prompt = prompt_row[0]
        if feature_description:
            prompt = f"Feature focus: {feature_description}\n\n{prompt}"
    finally:
        await db.close()

    cancel_event = asyncio.Event()
    active_jobs[job_id] = cancel_event
    task = asyncio.create_task(_start_job(job_id, repo_path, slug, prompt, max_iterations))
    running_tasks[job_id] = task

    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.get("/{job_id}", response_class=HTMLResponse)
async def job_viewer(request: Request, job_id: int):
    from clean_room.main import templates
    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
        job = await cursor.fetchone()
        cursor = await db.execute("SELECT * FROM repos WHERE id=?", (job["repo_id"],))
        repo = await cursor.fetchone()
        cursor = await db.execute("SELECT name FROM prompts WHERE id=?", (job["prompt_id"],))
        prompt_row = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT * FROM job_logs WHERE job_id=? ORDER BY id", (job_id,),
        )
        logs = await cursor.fetchall()
        return templates.TemplateResponse("job_viewer.html", {
            "request": request, "job": job, "repo": repo,
            "prompt_name": prompt_row[0], "logs": logs,
        })
    finally:
        await db.close()


@router.post("/{job_id}/stop", response_class=RedirectResponse)
async def stop_job(job_id: int):
    if job_id in active_jobs:
        active_jobs[job_id].set()
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@router.post("/{job_id}/restart", response_class=RedirectResponse)
async def restart_job(job_id: int):
    """Create a new job with the same parameters."""
    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
        old = await cursor.fetchone()
        cursor = await db.execute(
            "INSERT INTO jobs (repo_id, prompt_id, feature_description, max_iterations) "
            "VALUES (?, ?, ?, ?) RETURNING id",
            (old["repo_id"], old["prompt_id"], old["feature_description"],
             old["max_iterations"]),
        )
        row = await cursor.fetchone()
        new_id = row[0]
        await db.commit()

        cursor = await db.execute(
            "SELECT clone_path, slug FROM repos WHERE id=?", (old["repo_id"],),
        )
        repo_row = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT template FROM prompts WHERE id=?", (old["prompt_id"],),
        )
        prompt_row = await cursor.fetchone()

        repo_path = Path(repo_row[0])
        slug = repo_row[1]
        prompt = prompt_row[0]
        if old["feature_description"]:
            prompt = f"Feature focus: {old['feature_description']}\n\n{prompt}"
    finally:
        await db.close()

    cancel_event = asyncio.Event()
    active_jobs[new_id] = cancel_event
    task = asyncio.create_task(
        _start_job(new_id, repo_path, slug, prompt, old["max_iterations"])
    )
    running_tasks[new_id] = task

    return RedirectResponse(f"/jobs/{new_id}", status_code=303)


@router.get("/{job_id}/status", response_class=HTMLResponse)
async def job_status(request: Request, job_id: int):
    from clean_room.main import templates
    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
        job = await cursor.fetchone()
        cursor = await db.execute("SELECT name FROM prompts WHERE id=?", (job["prompt_id"],))
        prompt_row = await cursor.fetchone()
        response = templates.TemplateResponse("partials/job_status.html", {
            "request": request, "job": job, "prompt_name": prompt_row[0],
        })
        if job["status"] not in ("pending", "running"):
            response.headers["HX-Trigger"] = "jobFinished"
        return response
    finally:
        await db.close()


@router.get("/{job_id}/stream")
async def job_stream(job_id: int):
    async def event_generator():
        for msg in log_buffer.get_history(job_id):
            yield {"data": msg}
        if job_id not in log_buffer._closed:
            async for msg in log_buffer.subscribe(job_id):
                yield {"data": msg}
        yield {"event": "job-done", "data": ""}

    return EventSourceResponse(event_generator())
