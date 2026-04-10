from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse

from build_your_room.config import DB_PATH
from build_your_room.db import get_db

router = APIRouter(prefix="/prompts")


@router.get("", response_class=HTMLResponse)
async def list_prompts(request: Request):
    from build_your_room.main import templates

    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute("SELECT * FROM prompts ORDER BY id")
        prompts = await cursor.fetchall()
        return templates.TemplateResponse("prompts.html", {
            "request": request, "prompts": prompts,
        })
    finally:
        await db.close()


@router.post("", response_class=HTMLResponse)
async def create_prompt(
    request: Request,
    name: str = Form(),
    body: str = Form(),
    stage_type: str = Form("custom"),
    agent_type: str = Form("claude"),
):
    from build_your_room.main import templates

    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute(
            "INSERT INTO prompts (name, body, stage_type, agent_type) "
            "VALUES (?, ?, ?, ?) RETURNING *",
            (name, body, stage_type, agent_type),
        )
        prompt = await cursor.fetchone()
        await db.commit()
        return templates.TemplateResponse("partials/prompt_row.html", {
            "request": request, "prompt": prompt,
        })
    finally:
        await db.close()


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

    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute(
            "UPDATE prompts SET name=?, body=?, stage_type=?, agent_type=?, "
            "updated_at=datetime('now') WHERE id=? RETURNING *",
            (name, body, stage_type, agent_type, prompt_id),
        )
        prompt = await cursor.fetchone()
        await db.commit()
        return templates.TemplateResponse("partials/prompt_row.html", {
            "request": request, "prompt": prompt,
        })
    finally:
        await db.close()


@router.delete("/{prompt_id}", response_class=HTMLResponse)
async def delete_prompt(prompt_id: int):
    db = await get_db(DB_PATH)
    try:
        await db.execute("DELETE FROM prompts WHERE id=?", (prompt_id,))
        await db.commit()
        return HTMLResponse("")
    finally:
        await db.close()


@router.get("/{prompt_id}/edit", response_class=HTMLResponse)
async def edit_prompt_form(request: Request, prompt_id: int):
    from build_your_room.main import templates

    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,))
        prompt = await cursor.fetchone()
        return templates.TemplateResponse("partials/prompt_form.html", {
            "request": request, "prompt": prompt,
        })
    finally:
        await db.close()


@router.get("/{prompt_id}/row", response_class=HTMLResponse)
async def prompt_row(request: Request, prompt_id: int):
    from build_your_room.main import templates

    db = await get_db(DB_PATH)
    try:
        cursor = await db.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,))
        prompt = await cursor.fetchone()
        return templates.TemplateResponse("partials/prompt_row.html", {
            "request": request, "prompt": prompt,
        })
    finally:
        await db.close()
