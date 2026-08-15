from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.api.routes import router
from backend.config import settings
from backend.services.job_manager import JobManager
from backend.services.job_store import JobStore


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
for noisy_logger in ("httpx", "httpcore", "multipart", "watchfiles"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    store = JobStore(settings.database_path)
    interrupted = store.mark_interrupted_jobs()
    if interrupted:
        logging.getLogger(__name__).warning(
            "检测到 %d 个上次意外中断的任务，已保留缓存并标记为可重试",
            len(interrupted),
        )
    manager = JobManager(store, settings)
    app.state.job_store = store
    app.state.job_manager = manager
    yield
    await manager.shutdown()


app = FastAPI(
    title="AutoTranslate AI 视频中文化",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(router)
app.mount(
    "/",
    StaticFiles(directory=str(settings.project_root / "frontend"), html=True),
    name="frontend",
)
