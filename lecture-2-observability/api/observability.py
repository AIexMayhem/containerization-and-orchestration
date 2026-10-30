import json
import logging
import logging.config
import os
import time
from datetime import UTC, datetime

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CollectorRegistry, Counter, Histogram
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("api")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        context = trace.get_current_span().get_span_context()
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": f"{context.trace_id:032x}",
            "span_id": f"{context.span_id:016x}",
        }
        for field in ("event", "method", "route", "status", "duration_seconds"):
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"json": {"()": JsonFormatter}},
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                    "formatter": "json",
                }
            },
            "root": {"handlers": ["stdout"], "level": "INFO"},
            "loggers": {
                "uvicorn": {"handlers": [], "propagate": True},
                "uvicorn.error": {"handlers": [], "propagate": True},
                "uvicorn.access": {"handlers": [], "propagate": False},
                "httpx2": {"level": "WARNING"},
                "httpcore2": {"level": "WARNING"},
            },
        }
    )


def create_tracer_provider() -> TracerProvider:
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": os.getenv("OTEL_SERVICE_NAME", "api")}
        )
    )
    # Keep valid trace IDs without a backend; enable export when one is configured.
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
    ):
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return provider


class RequestObservabilityMiddleware:
    def __init__(self, app: ASGIApp, registry: CollectorRegistry) -> None:
        self.app = app
        labels = ("method", "route", "status")
        self.requests = Counter(
            "http_requests_total", "Completed HTTP requests", labels, registry=registry
        )
        self.errors = Counter(
            "http_errors_total",
            "HTTP requests with a 5xx response",
            labels,
            registry=registry,
        )
        self.duration = Histogram(
            "http_request_duration_seconds",
            "HTTP response time in seconds",
            labels,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 1.5, 2, 2.5, 3, 5),
            registry=registry,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/metrics":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 500

        async def send_response(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                context = trace.get_current_span().get_span_context()
                MutableHeaders(scope=message)["X-Trace-ID"] = f"{context.trace_id:032x}"
            await send(message)

        try:
            await self.app(scope, receive, send_response)
        except Exception:
            logger.exception("Unhandled request exception")
            raise
        finally:
            elapsed = time.perf_counter() - started
            route = getattr(scope.get("route"), "path", "unmatched")
            labels = (scope["method"], route, str(status))
            self.requests.labels(*labels).inc()
            if status >= 500:
                self.errors.labels(*labels).inc()
            self.duration.labels(*labels).observe(elapsed)
            logger.log(
                logging.ERROR if status >= 500 else logging.INFO,
                "HTTP request completed",
                extra={
                    "event": "http_request",
                    "method": scope["method"],
                    "route": route,
                    "status": status,
                    "duration_seconds": round(elapsed, 6),
                },
            )
