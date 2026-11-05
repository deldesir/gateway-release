from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.api.routes import router as chat_router
from app.api.personas import router as personas_router
from app.api.downloads import router as downloads_router
from app.db import init_db
from app.seed import seed_personas
import app.models  # Register SQLModel tables
# NOTE: Legacy app.commands import removed (ADR-011 migration).
# Commands are now macro_* tools in app/graph/tools/{system,config}.py


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.hooks.siyuan_tools import _init_notebook_map
    from app.hermes.tools import register_all_tools
    
    # Startup: Initialize DB, then seed default personas
    await init_db()
    await seed_personas()
    
    # V2 Init: Pre-load SiYuan configuration and register Hermes tools
    _init_notebook_map()
    register_all_tools()

    # SiYuan liveness gate — the wiki layer no-ops silently on failure, so a
    # broken auth code or kernel API change would otherwise go unnoticed.
    from app.hooks.siyuan_tools import siyuan_health
    from app.logger import logger as _logger
    _sy = siyuan_health()
    if not _sy["auth_ok"]:
        _logger.error(
            "════ SIYUAN INTEGRATION DOWN ════ "
            f"{_sy['error']} — all wiki reads/writes will silently no-op "
            "until this is fixed."
        )
    elif _sy["prerelease"]:
        _logger.warning(
            f"SiYuan kernel {_sy['kernel_version']} is a PRE-RELEASE — an API "
            "change could break the wiki layer; watch this after upgrades."
        )

    # V3 Init: Ensure FSRS mastery tables exist in PostgreSQL
    from app.plugins.social.mastery import ensure_tables as ensure_mastery_tables
    ensure_mastery_tables()
    
    # Background Task: Remove old analytics dumps (F-32)
    import asyncio
    async def _cleanup_dumps_loop():
        import os, time
        from app.logger import logger
        dumps_dir = "/opt/iiab/ai-gateway/data/dumps"
        while True:
            try:
                if os.path.exists(dumps_dir):
                    now = time.time()
                    for root, _, files in os.walk(dumps_dir):
                        for name in files:
                            filepath = os.path.join(root, name)
                            if now - os.path.getmtime(filepath) > 7 * 86400:  # 7 days
                                os.remove(filepath)
            except Exception as e:
                logger.warning(f"Dump TTL cleanup failed: {e}")
            await asyncio.sleep(86400)  # run daily
            
    cleanup_task = asyncio.create_task(_cleanup_dumps_loop())
    
    yield
    cleanup_task.cancel()
    # Shutdown: Cleanup if needed


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application.
    """
    app = FastAPI(
        title="Konex Pro Backend",
        version="0.1.0",
        description="Agental logic for Konex services (IIAB)",
        lifespan=lifespan
    )
    app.include_router(chat_router)
    app.include_router(personas_router, prefix="/v1", tags=["personas"])
    app.include_router(downloads_router)
    from app.api.adapters.bridge import router as bridge_router
    app.include_router(bridge_router)
    return app


app = create_app()
