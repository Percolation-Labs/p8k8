"""Task handlers — one module per task_type.

Each handler implements the TaskHandler protocol:
  async def handle(task: dict, ctx: WorkerContext) -> dict | None
"""

from p8.workers.handlers.dreaming import DreamingHandler
from p8.workers.handlers.file_processing import FileProcessingHandler
from p8.workers.handlers.reading import ReadingSummaryHandler
from p8.workers.handlers.scheduled import ScheduledHandler

__all__ = ["FileProcessingHandler", "DreamingHandler", "ReadingSummaryHandler", "ScheduledHandler"]
