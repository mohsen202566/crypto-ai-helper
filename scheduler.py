from __future__ import annotations

"""
30 - scheduler.py

Central async scheduler for the locked Movement Hunter architecture.

Responsibilities:
- Run periodic jobs safely:
  auto scan
  real position monitor
  ghost monitor
  health checks
  cleanup jobs
- Prevent overlapping execution of the same job.
- Keep bot alive when one job fails.
- Provide start/stop/status controls.
- Integrate with logger.py and error_handler.py.

Strictly forbidden:
- No trading decisions.
- No Toobit order placement.
- No Telegram formatting.
- No Paper mode.
- No Setup flow.

bot.py may use this scheduler instead of creating loops directly.
"""

from dataclasses import dataclass, asdict, field
from typing import Any, Awaitable, Callable, Dict, Optional
import asyncio
import time

from logger import info, warning
from error_handler import handle_error


AsyncJob = Callable[[], Awaitable[None]]


JOB_STOPPED = "STOPPED"
JOB_RUNNING = "RUNNING"
JOB_SLEEPING = "SLEEPING"
JOB_FAILED = "FAILED"


@dataclass
class ScheduledJob:
    name: str
    callback: AsyncJob
    interval_seconds: int
    enabled: bool = True
    run_immediately: bool = False
    status: str = JOB_STOPPED
    last_run: int = 0
    last_success: int = 0
    last_error: str = ""
    run_count: int = 0
    error_count: int = 0
    task: Optional[asyncio.Task] = field(default=None, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data.pop("task", None)
        data.pop("lock", None)
        return data


class Scheduler:
    """
    VPS-safe async scheduler.

    Each job has its own loop and lock, so slow jobs cannot overlap with
    themselves. Other jobs continue running if one fails.
    """

    def __init__(self):
        self.jobs: Dict[str, ScheduledJob] = {}
        self._started = False

    def add_job(
        self,
        name: str,
        callback: AsyncJob,
        interval_seconds: int,
        enabled: bool = True,
        run_immediately: bool = False,
    ) -> ScheduledJob:
        if interval_seconds < 1:
            interval_seconds = 1

        job = ScheduledJob(
            name=name,
            callback=callback,
            interval_seconds=interval_seconds,
            enabled=enabled,
            run_immediately=run_immediately,
        )
        self.jobs[name] = job

        if self._started and enabled:
            job.task = asyncio.create_task(self._job_loop(job))

        return job

    async def start(self) -> None:
        self._started = True
        for job in self.jobs.values():
            if job.enabled and job.task is None:
                job.task = asyncio.create_task(self._job_loop(job))
        info("scheduler", "started", {"jobs": list(self.jobs.keys())})

    async def stop(self) -> None:
        for job in self.jobs.values():
            job.enabled = False
            if job.task:
                job.task.cancel()

        for job in self.jobs.values():
            if job.task:
                try:
                    await job.task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                job.task = None
                job.status = JOB_STOPPED

        self._started = False
        info("scheduler", "stopped", {})

    def enable(self, name: str) -> bool:
        job = self.jobs.get(name)
        if not job:
            return False
        job.enabled = True
        if self._started and job.task is None:
            job.task = asyncio.create_task(self._job_loop(job))
        return True

    def disable(self, name: str) -> bool:
        job = self.jobs.get(name)
        if not job:
            return False
        job.enabled = False
        if job.task:
            job.task.cancel()
            job.task = None
        job.status = JOB_STOPPED
        return True

    def status(self) -> Dict[str, Any]:
        return {
            "started": self._started,
            "jobs": {name: job.to_dict() for name, job in self.jobs.items()},
        }

    async def _job_loop(self, job: ScheduledJob) -> None:
        if not job.run_immediately:
            job.status = JOB_SLEEPING
            await asyncio.sleep(job.interval_seconds)

        while job.enabled:
            try:
                async with job.lock:
                    job.status = JOB_RUNNING
                    job.last_run = int(time.time())
                    job.run_count += 1

                    await job.callback()

                    job.last_success = int(time.time())
                    job.last_error = ""
                    job.status = JOB_SLEEPING

            except asyncio.CancelledError:
                job.status = JOB_STOPPED
                raise
            except Exception as exc:
                job.error_count += 1
                job.last_error = str(exc)
                job.status = JOB_FAILED
                handle_error("scheduler:" + job.name, exc, job.to_dict())
                warning("scheduler", "job failed", {"job": job.name, "error": str(exc)})

            await asyncio.sleep(job.interval_seconds)


_default_scheduler: Optional[Scheduler] = None


def scheduler() -> Scheduler:
    global _default_scheduler
    if _default_scheduler is None:
        _default_scheduler = Scheduler()
    return _default_scheduler


def add_job(
    name: str,
    callback: AsyncJob,
    interval_seconds: int,
    enabled: bool = True,
    run_immediately: bool = False,
) -> ScheduledJob:
    return scheduler().add_job(
        name=name,
        callback=callback,
        interval_seconds=interval_seconds,
        enabled=enabled,
        run_immediately=run_immediately,
    )


async def start_scheduler() -> None:
    await scheduler().start()


async def stop_scheduler() -> None:
    await scheduler().stop()


def scheduler_status() -> Dict[str, Any]:
    return scheduler().status()
