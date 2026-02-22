"""Generic tiered worker — claims tasks from task_queue and dispatches to handlers.

Usage:
    python -m p8.workers.processor --tier small
    python -m p8.workers.processor --tier micro --poll-interval 5 --batch-size 1

Same Docker image for all workers — command override selects the tier.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from dataclasses import dataclass, field

from p8.services.bootstrap import bootstrap_services
from p8.utils.ids import short_id
from p8.services.database import Database
from p8.services.encryption import EncryptionService
from p8.services.queue import QueueService

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Handler protocol and registry
# ---------------------------------------------------------------------------


@dataclass
class WorkerContext:
    """Shared services passed to task handlers."""

    db: Database
    encryption: EncryptionService
    queue: QueueService
    worker_id: str
    tier: str
    file_service: object = None  # type: ignore[assignment]
    content_service: object = None  # type: ignore[assignment]
    settings: object = None  # type: ignore[assignment]


class TaskHandler:
    """Base class for task handlers. Subclass and implement handle()."""

    async def handle(self, task: dict, ctx: WorkerContext) -> dict | None:
        """Process a task. Return a result dict or None."""
        raise NotImplementedError


_HANDLER_REGISTRY: dict[str, TaskHandler] = {}


def register_handler(task_type: str, handler: TaskHandler) -> None:
    """Register a handler for a task type."""
    _HANDLER_REGISTRY[task_type] = handler
    log.info("Registered handler for task_type=%s: %s", task_type, type(handler).__name__)


def _register_default_handlers() -> None:
    """Register built-in handlers."""
    from p8.workers.handlers.dreaming import DreamingHandler
    from p8.workers.handlers.file_processing import FileProcessingHandler
    from p8.workers.handlers.news import NewsHandler
    from p8.workers.handlers.scheduled import ScheduledHandler

    if "file_processing" not in _HANDLER_REGISTRY:
        register_handler("file_processing", FileProcessingHandler())  # type: ignore[arg-type]
    if "dreaming" not in _HANDLER_REGISTRY:
        register_handler("dreaming", DreamingHandler())  # type: ignore[arg-type]
    if "news" not in _HANDLER_REGISTRY:
        register_handler("news", NewsHandler())  # type: ignore[arg-type]
    if "scheduled" not in _HANDLER_REGISTRY:
        register_handler("scheduled", ScheduledHandler())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TieredWorker
# ---------------------------------------------------------------------------


@dataclass
class TieredWorker:
    """Background worker that polls task_queue for a specific tier."""

    tier: str
    poll_interval: float = 5.0
    batch_size: int = 1
    worker_id: str = field(default_factory=lambda: short_id("worker-"))
    _running: bool = field(default=False, repr=False)

    async def run(self) -> None:
        """Bootstrap services and enter the poll loop."""
        _register_default_handlers()

        async with bootstrap_services() as (
            db, encryption, settings, file_service, content_service, _embedding_service, queue,
        ):
            ctx = WorkerContext(
                db=db,
                encryption=encryption,
                queue=queue,
                worker_id=self.worker_id,
                tier=self.tier,
            )

            ctx.file_service = file_service
            ctx.content_service = content_service
            ctx.settings = settings

            self._running = True
            log.info(
                "Worker %s started (tier=%s, poll=%.1fs, batch=%d)",
                self.worker_id, self.tier, self.poll_interval, self.batch_size,
            )

            while self._running:
                try:
                    tasks = await queue.claim(self.tier, self.worker_id, self.batch_size)
                    if not tasks:
                        await asyncio.sleep(self.poll_interval)
                        continue

                    for task in tasks:
                        await self._process_task(task, ctx, queue)

                except asyncio.CancelledError:
                    break
                except Exception:
                    log.exception("Worker %s poll error", self.worker_id)
                    await asyncio.sleep(self.poll_interval * 2)

            log.info("Worker %s stopped", self.worker_id)

    async def _process_task(self, task: dict, ctx: WorkerContext, queue: QueueService) -> None:
        """Dispatch a single task to its handler."""
        task_id = task["id"]
        task_type = task["task_type"]

        handler = _HANDLER_REGISTRY.get(task_type)
        if not handler:
            await queue.fail(task_id, f"No handler registered for task_type={task_type}")
            return

        # Pre-flight quota check
        if not await queue.check_task_quota(task):
            await queue.fail(task_id, "Quota exceeded")
            return

        try:
            result = await handler.handle(task, ctx)
            await queue.track_usage(task_id, result or {})
        except Exception as e:
            log.exception("Task %s (%s) failed", task_id, task_type)
            await queue.emit_event(
                task_id, "error",
                worker_id=self.worker_id,
                error=str(e),
                detail={"task_type": task_type, "handler": type(handler).__name__},
            )
            await queue.fail(task_id, str(e))

    def stop(self) -> None:
        """Signal the worker to stop after current iteration."""
        self._running = False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="QMS tiered worker")
    parser.add_argument("--tier", required=True, choices=["micro", "small", "medium", "large"])
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--batch-size", type=int, default=1)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    )

    worker = TieredWorker(
        tier=args.tier,
        poll_interval=args.poll_interval,
        batch_size=args.batch_size,
    )

    loop = asyncio.new_event_loop()

    def _shutdown(sig, _frame):
        log.info("Received %s, shutting down...", signal.Signals(sig).name)
        worker.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        loop.run_until_complete(worker.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
