import asyncio
from contextlib import asynccontextmanager
from typing import Annotated

import httpx2 as httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPX2ClientInstrumentor
from opentelemetry.trace import Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from observability import (
    RequestObservabilityMiddleware,
    configure_logging,
    create_tracer_provider,
    logger,
)


def create_app() -> FastAPI:
    configure_logging()
    registry = CollectorRegistry()
    provider = create_tracer_provider()
    tracer = provider.get_tracer("api")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            async with httpx.AsyncClient(
                timeout=10,
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=100),
                trust_env=False,
            ) as client:
                HTTPX2ClientInstrumentor.instrument_client(
                    client, tracer_provider=provider
                )
                app.state.http_client = client
                yield
        finally:
            # BatchSpanProcessor flushes pending spans on shutdown.
            await asyncio.to_thread(provider.shutdown)

    app = FastAPI(title="Lab 2 Observability API", version="0.1.0", lifespan=lifespan)
    app.state.tracer_provider = provider
    app.add_middleware(RequestObservabilityMiddleware, registry=registry)

    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "ok"

    @app.get("/fail", status_code=500)
    async def fail() -> JSONResponse:
        trace.get_current_span().set_status(
            Status(StatusCode.ERROR, "Deliberate failure")
        )
        logger.error("Deliberate failure", extra={"event": "simulated_failure"})
        return JSONResponse({"detail": "Deliberate failure"}, status_code=500)

    @app.get("/slow")
    async def slow(
        seconds: Annotated[float, Query(ge=1, le=3, allow_inf_nan=False)] = 2,
    ) -> dict:
        with tracer.start_as_current_span("slow-op") as span:
            span.set_attribute("slow.seconds", seconds)
            logger.info("Starting slow operation", extra={"event": "slow_operation"})
            await asyncio.sleep(seconds)
        return {"slept_seconds": seconds}

    @app.get("/load")
    async def load(
        request: Request,
        count: Annotated[int, Query(ge=1, le=1000)] = 100,
        concurrency: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> JSONResponse:
        # ASGI server address is trusted; the incoming Host header is not.
        port = request.scope["server"][1]
        target = f"http://127.0.0.1:{port}/health"
        semaphore = asyncio.Semaphore(concurrency)

        async def hit_health() -> bool:
            async with semaphore:
                try:
                    response = await request.app.state.http_client.get(target)
                    return response.status_code == 200
                except httpx.RequestError:
                    logger.exception("Self-request failed")
                    return False

        results = await asyncio.gather(*(hit_health() for _ in range(count)))
        succeeded = sum(results)
        failed = count - succeeded
        return JSONResponse(
            {"requested": count, "succeeded": succeeded, "failed": failed},
            status_code=503 if failed else 200,
        )

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(
            generate_latest(registry), headers={"Content-Type": CONTENT_TYPE_LATEST}
        )

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls="/metrics$",
        exclude_spans=["receive", "send"],
    )
    return app
