import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)


class _HealthAccessLogFilter(logging.Filter):
    """Drop uvicorn access-log lines for /health and HEAD / probes (Render heartbeat)."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "/health" in message or 'HEAD / HTTP' in message:
            return False
        return True


logging.getLogger("uvicorn.access").addFilter(_HealthAccessLogFilter())
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .admin import router as admin_router, sync_router as admin_sync_router
from .database import create_tables, _tenant_pools
from .routers.onboarding import router as onboarding_router
from .webhook import router as webhook_router

logger = logging.getLogger(__name__)
STARTUP_DB_INIT_TIMEOUT_SECONDS = float(os.getenv("STARTUP_DB_INIT_TIMEOUT_SECONDS", "15"))


# ── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────
    # Load learned intent rules and summary patterns from disk
    from .services.runtime_memory import load_memory
    load_memory()

    async def _initialize_db_metadata() -> None:
        timeout_seconds = max(1.0, STARTUP_DB_INIT_TIMEOUT_SECONDS)
        try:
            await asyncio.wait_for(create_tables(), timeout=timeout_seconds)
            logger.info("Database table check completed during startup.")
        except TimeoutError:
            logger.warning(
                "Database table check timed out after %.1fs; continuing startup to serve health checks.",
                timeout_seconds,
            )
        except Exception:
            logger.exception("Failed to create database tables on startup.")

    # Do not block ASGI startup on database availability;
    # Render health checks should pass quickly.
    asyncio.create_task(_initialize_db_metadata())

    async def _start_main_db_sync() -> None:
        try:
            from .sync.main_db_sync import start_sync_scheduler
            await start_sync_scheduler()
        except Exception:
            logger.exception("Failed to start Botivate Main DB sync scheduler.")

    asyncio.create_task(_start_main_db_sync())

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────
    from .sync.main_db_sync import stop_sync_scheduler

    try:
        await stop_sync_scheduler()
    except Exception:
        logger.exception("Error stopping Botivate Main DB sync scheduler.")

    for tid, pool in list(_tenant_pools.items()):
        try:
            await asyncio.wait_for(pool.close(), timeout=5.0)
            logger.info("Closed tenant pool for %s", tid)
        except TimeoutError:
            logger.warning("Pool close timed out for %s, terminating", tid)
            pool.terminate()
        except Exception:
            logger.exception("Error closing tenant pool %s", tid)
    _tenant_pools.clear()


# ── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(title="botivate-bot", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhook_router)
app.include_router(admin_router)
app.include_router(admin_sync_router)
app.include_router(onboarding_router)

static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok", "service": "botivate-bot"}


@app.head("/")
async def root_head() -> Response:
    return Response(status_code=200)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
