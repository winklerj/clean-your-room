from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse

from build_your_room.db import get_pool

router = APIRouter(prefix="/prompts")


@router.get("", response_class=HTMLResponse)
async def list_prompts(request: Request):
    from build_your_room.main import templates

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM prompts ORDER BY id")
        prompts = await cur.fetchall()
    return templates.TemplateResponse("prompts.html", {
        "request": request, "prompts": prompts,
    })


@router.post("", response_class=HTMLResponse)
async def create_prompt(
    request: Request,
    name: str = Form(),
    body: str = Form(),
    stage_type: str = Form("custom"),
    agent_type: str = Form("claude"),
):
    from build_your_room.main import templates

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO prompts (name, body, stage_type, agent_type) "
            "VALUES (%s, %s, %s, %s) RETURNING *",
            (name, body, stage_type, agent_type),
        )
        prompt = await cur.fetchone()
        await conn.commit()
    return templates.TemplateResponse("partials/prompt_row.html", {
        "request": request, "prompt": prompt,
    })


@router.put("/{prompt_id}", response_class=HTMLResponse)
async def update_prompt(
    request: Request,
    prompt_id: int,
    name: str = Form(),
    body: str = Form(),
    stage_type: str = Form("custom"),
    agent_type: str = Form("claude"),
):
    from build_your_room.main import templates

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE prompts SET name=%s, body=%s, stage_type=%s, agent_type=%s, "
            "updated_at=now() WHERE id=%s RETURNING *",
            (name, body, stage_type, agent_type, prompt_id),
        )
        prompt = await cur.fetchone()
        await conn.commit()
    return templates.TemplateResponse("partials/prompt_row.html", {
        "request": request, "prompt": prompt,
    })


@router.delete("/{prompt_id}", response_class=HTMLResponse)
async def delete_prompt(prompt_id: int):
    pool = get_pool()
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM prompts WHERE id=%s", (prompt_id,))
        await conn.commit()
    return HTMLResponse("")


@router.get("/{prompt_id}/edit", response_class=HTMLResponse)
async def edit_prompt_form(request: Request, prompt_id: int):
    from build_your_room.main import templates

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM prompts WHERE id=%s", (prompt_id,))
        prompt = await cur.fetchone()
    return templates.TemplateResponse("partials/prompt_form.html", {
        "request": request, "prompt": prompt,
    })


@router.get("/{prompt_id}/row", response_class=HTMLResponse)
async def prompt_row(request: Request, prompt_id: int):
    from build_your_room.main import templates

    pool = get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM prompts WHERE id=%s", (prompt_id,))
        prompt = await cur.fetchone()
    return templates.TemplateResponse("partials/prompt_row.html", {
        "request": request, "prompt": prompt,
    })
