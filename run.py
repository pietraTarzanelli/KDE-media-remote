import uvicorn
from app.config import main_config

cfg = main_config()["server"]

uvicorn.run(
    "app.main:app",
    host=str(cfg["host"]),
    port=int(cfg["port"]),
    reload=False,
)
