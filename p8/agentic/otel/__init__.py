"""OpenTelemetry instrumentation for p8 agents."""

from p8.agentic.otel.setup import get_current_trace_context, setup_instrumentation

__all__ = ["setup_instrumentation", "get_current_trace_context"]
