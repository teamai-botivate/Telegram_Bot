import logging

from fastapi import FastAPI

from .admin import router as admin_router
from .database import create_tables
from .webhook import router as webhook_router

logger = logging.getLogger(__name__)

app = FastAPI(title="botivate-bot")
app.include_router(webhook_router)
app.include_router(admin_router)


@app.on_event("startup")
async def startup() -> None:
	try:
		await create_tables()
	except Exception:
		logger.exception("Failed to create database tables on startup.")
