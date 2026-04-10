import uvicorn

from build_your_room.config import DEFAULT_PORT

uvicorn.run("build_your_room.main:app", host="127.0.0.1", port=DEFAULT_PORT, reload=True)
