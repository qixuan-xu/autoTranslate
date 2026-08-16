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
        task.add_done_callback(
            lambda completed, current_job_id=job_id: self._task_finished(
                current_job_id, completed
            )
        )

    def _task_finished(self, job_id: str, completed: asyncio.Task[None]) -> None:
        # A paused job can be resumed as soon as its prior task completes.  Do
        # not let the prior task's callback remove the newly-created task.
        if self.tasks.get(job_id) is completed:
            self.tasks.pop(job_id, None)

    async def _run(self, job_id: str) -> None:
        from backend.pipeline.runner import (
            PipelineAwaitingReference,
            PipelineCancelled,
            PipelineRunner,
        )

        runner = PipelineRunner(self.config)

        async def update(**fields: Any) -> dict[str, Any]:
            return await self.update_job(job_id, **fields)

        async def add_log(message: str, level: str = "INFO") -> None:
            await self.add_log(job_id, message, level)

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
            await add_log("全部处理完成")
            await update(
                status="completed",
                progress=100.0,
                current_step="处理完成",
                result_json=result,
            )
        except PipelineAwaitingReference as exc:
            await add_log("转写和字幕整理已完成，请选择参考声音片段", "INFO")
            await update(
                status="awaiting_reference",
                error=None,
                current_step="等待选择参考声音片段",
                step_index=4,
                progress=round(4 / 9 * 100, 2),
                segments_done=0,
                segments_total=exc.segment_count,
            )
        except PipelineCancelled:
            await add_log("任务已取消", "WARNING")
            await update(status="cancelled", current_step="已取消")
        except asyncio.CancelledError:
            await add_log("任务已取消", "WARNING")
            await update(status="cancelled", current_step="已取消")
            raise
        except Exception as exc:
            logger.exception("job %s failed", job_id)
            await add_log(str(exc), "ERROR")
            await update(status="failed", error=str(exc), current_step="处理失败")

    async def update_job(self, job_id: str, **fields: Any) -> dict[str, Any]:
        """Persist and publish one lightweight state snapshot without log history."""

        job = self.store.update_job(job_id, **fields)
        await self.publish(job_id, job)
        return job

    async def add_log(self, job_id: str, message: str, level: str = "INFO") -> None:
        """Persist and publish only the newly-created log row."""

        entry = self.store.add_log(job_id, message, level)
        if entry is not None:
            await self._publish_event(job_id, "log", {"log": entry})

    async def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id, include_logs=False)
        if job["status"] in TERMINAL_STATUSES:
            return job
        if job["status"] == "awaiting_reference":
            await self.add_log(job_id, "任务已取消", "WARNING")
            return await self.update_job(
                job_id,
                status="cancelled",
                current_step="已取消",
                cancel_requested=1,
            )
        job = self.store.update_job(job_id, cancel_requested=1, current_step="正在取消…")
        task = self.tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        await self.publish(job_id, job)
        return job

    def retry(self, job_id: str) -> None:
        job = self.store.get_job(job_id, include_logs=False)
        if job["status"] == "awaiting_reference":
            raise RuntimeError("任务正在等待参考片段，请使用 reference 接口继续")
        if job["status"] not in {"failed", "cancelled", "completed"}:
            raise RuntimeError("只有失败、取消或已完成的任务可以重跑")
        self.start(job_id)

    async def resume_reference(self, job_id: str) -> None:
        """Resume a paused job after its reference segment was persisted."""

        job = self.store.get_job(job_id, include_logs=False)
        if job["status"] != "queued" or job["settings"].get("reference_segment_id") is None:
            raise RuntimeError("任务的参考片段尚未完成保存")
        previous = self.tasks.get(job_id)
        if previous and not previous.done():
            await asyncio.shield(previous)
        job = self.store.get_job(job_id, include_logs=False)
        if job["status"] != "queued":
            raise RuntimeError("任务状态已变更，无法继续参考片段选择")
        self.start(job_id)

    async def publish(self, job_id: str, job: dict[str, Any] | None = None) -> None:
        if job is None:
            job = self.store.get_job(job_id, include_logs=False)
        # Be defensive if an older caller supplies a full GET response.
        # Historical logs belong only in GET and the initial SSE snapshot.
        if "logs" in job:
            job = {name: value for name, value in job.items() if name != "logs"}
        await self._publish_event(job_id, "update", job)

    async def _publish_event(
        self,
        job_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        event = {"type": event_type, "data": payload}
        for queue in tuple(self.listeners.get(job_id, ())):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

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
