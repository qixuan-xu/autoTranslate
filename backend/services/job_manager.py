from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

from backend.config import Settings
from backend.services.job_store import JobStore, TERMINAL_STATUSES


logger = logging.getLogger(__name__)


class JobManager:
    def __init__(self, store: JobStore, config: Settings):
        self.store = store
        self.config = config
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.listeners: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    def start(self, job_id: str) -> None:
        task = self.tasks.get(job_id)
        if task and not task.done():
            raise RuntimeError("该任务正在运行，请勿重复启动")
        self.store.update_job(
            job_id,
            cancel_requested=0,
            error=None,
            status="queued",
            progress=0.0,
            current_step="等待处理",
            step_index=0,
            segments_done=0,
            segments_total=0,
            result_json={},
        )
        task = asyncio.create_task(self._run(job_id), name=f"dub-job-{job_id}")
        self.tasks[job_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(job_id, None))

    async def _run(self, job_id: str) -> None:
        from backend.pipeline.runner import PipelineCancelled, PipelineRunner

        runner = PipelineRunner(self.config)

        async def update(**fields: Any) -> dict[str, Any]:
            job = self.store.update_job(job_id, **fields)
            await self.publish(job_id, job)
            return job

        async def add_log(message: str, level: str = "INFO") -> None:
            self.store.add_log(job_id, message, level)
            await self.publish(job_id)

        try:
            await update(status="running", error=None)
            await add_log("任务开始")
            job = self.store.get_job(job_id)
            result = await runner.run(
                job,
                update=update,
                log=add_log,
                is_cancelled=lambda: self.store.is_cancel_requested(job_id),
            )
            await update(
                status="completed",
                progress=100.0,
                current_step="处理完成",
                result_json=result,
            )
            await add_log("全部处理完成")
        except PipelineCancelled:
            await update(status="cancelled", current_step="已取消")
            await add_log("任务已取消", "WARNING")
        except asyncio.CancelledError:
            await update(status="cancelled", current_step="已取消")
            await add_log("任务已取消", "WARNING")
            raise
        except Exception as exc:
            logger.exception("job %s failed", job_id)
            await update(status="failed", error=str(exc), current_step="处理失败")
            await add_log(str(exc), "ERROR")

    async def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job["status"] in TERMINAL_STATUSES:
            return job
        job = self.store.update_job(job_id, cancel_requested=1, current_step="正在取消…")
        task = self.tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        await self.publish(job_id, job)
        return job

    def retry(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if job["status"] not in {"failed", "cancelled", "completed"}:
            raise RuntimeError("只有失败、取消或已完成的任务可以重跑")
        self.start(job_id)

    async def publish(self, job_id: str, job: dict[str, Any] | None = None) -> None:
        if job is None:
            job = self.store.get_job(job_id)
        for queue in tuple(self.listeners.get(job_id, ())):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(job)

    def subscribe(self, job_id: str) -> asyncio.Queue[dict[str, Any]]:
        self.store.get_job(job_id, include_logs=False)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10)
        self.listeners[job_id].add(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.listeners[job_id].discard(queue)
        if not self.listeners[job_id]:
            self.listeners.pop(job_id, None)

    async def shutdown(self) -> None:
        active = [task for task in self.tasks.values() if not task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
