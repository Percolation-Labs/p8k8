"""OpenTelemetry instrumentation setup for p8 agents.

Provides:
- OTLP exporter configuration (HTTP or gRPC)
- SanitizingSpanExporter (handles None attribute values)
- OpenInference span processor for pydantic-ai (optional)
- Idempotent setup (safe to call multiple times)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_instrumentation_initialized = False


def setup_instrumentation() -> None:
    """Initialize OpenTelemetry instrumentation.

    Idempotent — safe to call multiple times, only initializes once.
    Reads settings from ``p8.settings.Settings`` (P8_OTEL_* env vars).
    """
    global _instrumentation_initialized

    if _instrumentation_initialized:
        return

    from p8.settings import get_settings

    s = get_settings()
    if not s.otel_enabled:
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as HTTPExporter,
        )
        from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            SpanExporter,
            SpanExportResult,
        )

        # ------------------------------------------------------------------
        # SanitizingSpanExporter — wraps a real exporter, strips None values
        # that cause OTLP encoding failures.
        # ------------------------------------------------------------------

        class SanitizingSpanExporter(SpanExporter):
            def __init__(self, wrapped: SpanExporter):
                self._wrapped = wrapped

            @staticmethod
            def _sanitize(value):
                if value is None:
                    return ""
                if isinstance(value, dict):
                    return {k: SanitizingSpanExporter._sanitize(v) for k, v in value.items()}
                if isinstance(value, (list, tuple)):
                    return [SanitizingSpanExporter._sanitize(v) for v in value]
                return value

            def export(self, spans: tuple[ReadableSpan, ...]) -> SpanExportResult:
                sanitized = []
                for span in spans:
                    if span.attributes:
                        attrs = {k: self._sanitize(v) for k, v in span.attributes.items()}
                        sanitized.append(_SanitizedSpan(span, attrs))
                    else:
                        sanitized.append(span)
                return self._wrapped.export(tuple(sanitized))

            def shutdown(self) -> None:
                self._wrapped.shutdown()

            def force_flush(self, timeout_millis: int = 30000) -> bool:
                return self._wrapped.force_flush(timeout_millis)

        class _SanitizedSpan(ReadableSpan):
            """ReadableSpan wrapper with sanitized attributes."""

            def __init__(self, original: ReadableSpan, sanitized_attributes: dict):
                self._original = original
                self._sanitized_attributes = sanitized_attributes

            @property
            def name(self):
                return self._original.name

            @property
            def context(self):
                return self._original.context

            @property
            def parent(self):
                return self._original.parent

            @property
            def resource(self):
                return self._original.resource

            @property
            def instrumentation_scope(self):
                return self._original.instrumentation_scope

            @property
            def status(self):
                return self._original.status

            @property
            def start_time(self):
                return self._original.start_time

            @property
            def end_time(self):
                return self._original.end_time

            @property
            def links(self):
                return self._original.links

            @property
            def events(self):
                return self._original.events

            @property
            def kind(self):
                return self._original.kind

            @property
            def attributes(self):
                return self._sanitized_attributes

            @property
            def dropped_attributes(self):
                return self._original.dropped_attributes

            @property
            def dropped_events(self):
                return self._original.dropped_events

            @property
            def dropped_links(self):
                return self._original.dropped_links

            def get_span_context(self):
                return self._original.get_span_context()

        # ------------------------------------------------------------------
        # TracerProvider + exporter
        # ------------------------------------------------------------------

        resource = Resource(attributes={
            SERVICE_NAME: s.otel_service_name,
            DEPLOYMENT_ENVIRONMENT: "production",
        })

        tracer_provider = TracerProvider(resource=resource)

        if s.otel_protocol == "grpc":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter as GRPCExporter,
            )
            base_exporter = GRPCExporter(
                endpoint=s.otel_collector_endpoint,
                timeout=s.otel_export_timeout,
                insecure=s.otel_insecure,
            )
        else:
            base_exporter = HTTPExporter(
                endpoint=f"{s.otel_collector_endpoint}/v1/traces",
                timeout=s.otel_export_timeout,
            )

        exporter = SanitizingSpanExporter(base_exporter)
        tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(tracer_provider)

        logger.info(
            "OTLP exporter configured: %s (%s)", s.otel_collector_endpoint, s.otel_protocol
        )

        # OpenInference span processor for pydantic-ai (optional)
        try:
            from openinference.instrumentation.pydantic_ai import (
                OpenInferenceSpanProcessor as PydanticAISpanProcessor,
            )
            tracer_provider.add_span_processor(PydanticAISpanProcessor())
            logger.info("Added OpenInference span processor for pydantic-ai")
        except ImportError:
            pass

        _instrumentation_initialized = True
        logger.info("OpenTelemetry instrumentation initialized")

    except Exception:
        logger.exception("Failed to initialize OTEL instrumentation")


def get_current_trace_context() -> tuple[str | None, str | None]:
    """Return (trace_id, span_id) hex strings from the current OTEL context.

    Returns (None, None) if OTEL is not active or no span is recording.
    """
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.trace_id:
            return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
    except Exception:
        pass
    return None, None
