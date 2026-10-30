"""Local metrics exporter and persistent collection-event HTTP interface."""

import argparse
import asyncio
import json
import math
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import uvicorn
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tender_mcp.metrics_probes import ProbeSettings, build_probes
from tender_mcp.metrics_store import EventConflict, EventStore

COMPONENTS = ("documents", "reference", "n8n", "ollama")
Probe = Callable[[], Awaitable[bool]]
Timestamp = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow: Literal["tender-precheck"]
    execution_id: Annotated[
        str,
        Field(strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"),
    ]


class _Start(_Event):
    started_at: Timestamp


class _Finish(_Event):
    finished_at: Timestamp
    outcome: Literal["complete", "incomplete", "error"]


class _Readiness:
    def __init__(self):
        self._values: dict[str, tuple[bool, float]] = {}
        self._lock = threading.Lock()

    def update(self, component: str, ready: bool, checked_at: float):
        with self._lock:
            self._values[component] = (ready, checked_at)

    def snapshot(self):
        with self._lock:
            return dict(self._values)


class _Metrics:
    def __init__(self, store: EventStore, readiness: _Readiness, clock: Callable[[], float]):
        self.store = store
        self.readiness = readiness
        self.clock = clock

    def collect(self):
        ready = GaugeMetricFamily(
            "tender_dependency_ready", "Last dependency probe succeeded", labels=["component"]
        )
        checked = GaugeMetricFamily(
            "tender_dependency_last_probe_timestamp_seconds",
            "Unix time of the last completed dependency probe",
            labels=["component"],
        )
        for component, (available, checked_at) in self.readiness.snapshot().items():
            ready.add_metric([component], int(available))
            checked.add_metric([component], checked_at)
        yield ready
        yield checked
        counts = CounterMetricFamily(
            "tender_collection_runs_total",
            "Terminal collection outcomes recorded once per execution",
            labels=["outcome"],
        )
        for outcome, total in self.store.totals().items():
            counts.add_metric([outcome], total)
        yield counts
        active = GaugeMetricFamily(
            "tender_collection_oldest_running_age_seconds",
            "Age of the oldest collection without a recorded finish; zero when none",
        )
        active.add_metric([], self.store.oldest_running_age(self.clock()))
        yield active


def create_app(
    store: EventStore,
    *,
    probes: Mapping[str, Probe],
    probe_interval: float = 15.0,
    probe_timeout: float = 5.0,
    clock: Callable[[], float] = time.time,
) -> Starlette:
    if not set(probes).issubset(COMPONENTS):
        raise ValueError("Unknown dependency component")
    if not all(math.isfinite(value) and value > 0 for value in (probe_interval, probe_timeout)):
        raise ValueError("Probe interval and timeout must be finite and positive")
    readiness = _Readiness()
    registry = CollectorRegistry()
    registry.register(_Metrics(store, readiness, clock))

    async def probe_loop(component: str, probe: Probe):
        while True:
            try:
                async with asyncio.timeout(probe_timeout):
                    available = await probe()
            except Exception:
                available = False
            readiness.update(component, available is True, clock())
            await asyncio.sleep(probe_interval)

    @asynccontextmanager
    async def lifespan(app):
        tasks = [asyncio.create_task(probe_loop(name, probe)) for name, probe in probes.items()]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def metrics(request: Request):
        body = await asyncio.to_thread(generate_latest, registry)
        return Response(body, headers={"Content-Type": CONTENT_TYPE_LATEST})

    async def event(request: Request):
        model = _Start if request.url.path == "/events/start" else _Finish
        try:
            payload = model.model_validate(await request.json())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"detail": "Request body must be JSON"}, status_code=400)
        except ValidationError as error:
            return JSONResponse(
                {"detail": error.errors(include_url=False, include_input=False)}, status_code=422
            )
        try:
            if isinstance(payload, _Start):
                await asyncio.to_thread(
                    store.record_start, payload.workflow, payload.execution_id, payload.started_at
                )
            else:
                await asyncio.to_thread(
                    store.record_finish,
                    payload.workflow,
                    payload.execution_id,
                    payload.finished_at,
                    payload.outcome,
                )
        except EventConflict as error:
            return JSONResponse({"detail": str(error)}, status_code=409)
        return JSONResponse({"accepted": True})

    return Starlette(
        routes=[
            Route("/metrics", metrics),
            Route("/events/start", event, methods=["POST"]),
            Route("/events/finish", event, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


def main():
    defaults = ProbeSettings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9108)
    parser.add_argument("--db-path", type=Path, default=Path(".state/metrics.sqlite3"))
    parser.add_argument("--probe-interval", type=float, default=15.0)
    parser.add_argument("--probe-timeout", type=float, default=5.0)
    for name in (
        "documents_url",
        "reference_url",
        "n8n_url",
        "ollama_url",
        "model",
        "tender_id",
        "package_version",
        "company_id",
        "profile_version",
    ):
        parser.add_argument("--" + name.replace("_", "-"), default=getattr(defaults, name))
    args = parser.parse_args()
    settings = ProbeSettings(
        documents_url=args.documents_url,
        reference_url=args.reference_url,
        n8n_url=args.n8n_url,
        ollama_url=args.ollama_url,
        model=args.model,
        tender_id=args.tender_id,
        package_version=args.package_version,
        company_id=args.company_id,
        profile_version=args.profile_version,
        timeout=args.probe_timeout,
    )
    app = create_app(
        EventStore(args.db_path),
        probes=build_probes(settings),
        probe_interval=args.probe_interval,
        probe_timeout=args.probe_timeout,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
