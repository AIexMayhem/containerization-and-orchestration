import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx2 as httpx
from conftest import serve
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from test_api import sample_value

from main import create_app


def test_load_sends_real_http_requests_to_self_and_propagates_trace(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    app = create_app()
    exporter = InMemorySpanExporter()
    app.state.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    with serve(app) as url, httpx.Client(base_url=url, trust_env=False) as client:
        response = client.get(
            "/load?count=12&concurrency=3", headers={"Host": "untrusted.invalid"}
        )
        assert response.status_code == 200
        assert response.json() == {"requested": 12, "succeeded": 12, "failed": 0}
        assert (
            sample_value(
                client,
                "http_requests_total",
                {"method": "GET", "route": "/health", "status": "200"},
            )
            == 12
        )

    spans = exporter.get_finished_spans()
    server_spans = [span for span in spans if span.kind == SpanKind.SERVER]
    client_spans = [span for span in spans if span.kind == SpanKind.CLIENT]
    assert len(server_spans) == 13
    assert len(client_spans) == 12
    assert {f"{span.context.trace_id:032x}" for span in spans} == {
        response.headers["x-trace-id"]
    }


def test_slow_does_not_block_health_requests(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    app = create_app()
    sleeping = threading.Event()

    class SlowStartObserver(SpanProcessor):
        def on_start(self, span, parent_context=None):
            if span.name == "slow-op":
                sleeping.set()

    app.state.tracer_provider.add_span_processor(SlowStartObserver())
    with (
        serve(app) as url,
        httpx.Client(base_url=url, trust_env=False) as client,
        ThreadPoolExecutor() as pool,
    ):
        slow = pool.submit(client.get, "/slow?seconds=1")
        assert sleeping.wait(timeout=3)
        started = time.perf_counter()
        assert client.get("/health").text == "ok"
        assert time.perf_counter() - started < 0.75
        assert not slow.done()
        assert slow.result().status_code == 200


def test_otlp_http_export_delivers_slow_and_failed_spans(monkeypatch):
    payloads = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = ExportTraceServiceRequest.FromString(
                self.rfile.read(int(self.headers["Content-Length"]))
            )
            payloads.append((self.path, payload))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Receiver) as receiver:
        thread = threading.Thread(target=receiver.serve_forever)
        thread.start()
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            f"http://127.0.0.1:{receiver.server_port}",
        )
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
        monkeypatch.setenv("OTEL_SERVICE_NAME", "lab2-api-test")
        try:
            with serve(create_app()) as url, httpx.Client(trust_env=False) as client:
                assert client.get(f"{url}/slow?seconds=1").status_code == 200
                assert client.get(f"{url}/fail").status_code == 500
            # Lifespan shutdown flushes the batch exporter before returning.
            assert payloads
            assert all(path == "/v1/traces" for path, _ in payloads)
            resources = [
                resource
                for _, payload in payloads
                for resource in payload.resource_spans
            ]
            assert any(
                attribute.key == "service.name"
                and attribute.value.string_value == "lab2-api-test"
                for resource in resources
                for attribute in resource.resource.attributes
            )
            spans = [
                span
                for resource in resources
                for scope in resource.scope_spans
                for span in scope.spans
            ]
            assert any(span.name == "slow-op" for span in spans)
            assert any(
                span.name == "GET /fail" and span.status.code == 2 for span in spans
            )
        finally:
            receiver.shutdown()
            thread.join(timeout=5)
