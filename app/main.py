import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .admin import router as admin_router
from .database import create_tables
from .webhook import router as webhook_router

logger = logging.getLogger(__name__)

app = FastAPI(title="botivate-bot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhook_router)
app.include_router(admin_router)

static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.on_event("startup")
async def startup() -> None:
	try:
		await create_tables()
	except Exception:
		logger.exception("Failed to create database tables on startup.")


@app.get("/health")
async def health() -> dict[str, str]:
	return {"status": "ok"}
