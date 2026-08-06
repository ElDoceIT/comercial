from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes.db import router as db_router
from app.api.routes.drive import router as drive_router
from app.api.routes.pages import router as pages_router
from app.api.routes.processing import router as processing_router
from app.core.config import get_settings


settings = get_settings()
BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title=settings.app_name)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(pages_router)
app.include_router(db_router)
app.include_router(drive_router)
app.include_router(processing_router)
