import uvicorn

from clean_room.config import DEFAULT_PORT

uvicorn.run("clean_room.main:app", host="127.0.0.1", port=DEFAULT_PORT, reload=True)
