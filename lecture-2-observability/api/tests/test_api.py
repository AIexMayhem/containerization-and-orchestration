import json
import time

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from prometheus_client.parser import text_string_to_metric_families

from main import create_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    with TestClient(create_app()) as test_client:
        yield test_client


def sample_value(client, name, labels):
    samples = [
        sample.value
        for family in text_string_to_metric_families(client.get("/metrics").text)
        for sample in family.samples
        if sample.name == name and sample.labels == labels
    ]
    return sum(samples)


def test_health_returns_plain_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.text == "ok"
    assert response.headers["content-type"].startswith("text/plain")


def test_fail_updates_red_metrics_and_marks_server_span_failed(client):
    exporter = InMemorySpanExporter()
    client.app.state.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))

    response = client.get("/fail")

    assert response.status_code == 500
    labels = {"method": "GET", "route": "/fail", "status": "500"}
    assert sample_value(client, "http_requests_total", labels) == 1
    assert sample_value(client, "http_errors_total", labels) == 1
    assert sample_value(client, "http_request_duration_seconds_count", labels) == 1
    spans = [
        span for span in exporter.get_finished_spans() if span.kind == SpanKind.SERVER
    ]
    assert len(spans) == 1
    assert spans[0].status.status_code == StatusCode.ERROR


def test_slow_records_actual_wait_in_nested_span_and_histogram(client):
    exporter = InMemorySpanExporter()
    client.app.state.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    started = time.perf_counter()

    response = client.get("/slow?seconds=1")

    assert response.status_code == 200
    assert 1 <= time.perf_counter() - started < 4
    spans = exporter.get_finished_spans()
    slow = next(span for span in spans if span.name == "slow-op")
    server = next(span for span in spans if span.kind == SpanKind.SERVER)
    assert slow.parent.span_id == server.context.span_id
    assert slow.context.trace_id == server.context.trace_id
    assert (slow.end_time - slow.start_time) / 1e9 >= 1
    labels = {"method": "GET", "route": "/slow", "status": "200"}
    assert sample_value(client, "http_request_duration_seconds_count", labels) == 1
    assert sample_value(client, "http_request_duration_seconds_sum", labels) >= 1


def test_json_logs_correlate_with_incoming_trace_context(capsys, monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    trace_id = "1234567890abcdef1234567890abcdef"
    # Bind the log handler during the call phase, after capsys starts capturing.
    with TestClient(create_app()) as client:
        response = client.get(
            "/fail", headers={"traceparent": f"00-{trace_id}-1234567890abcdef-01"}
        )

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    request_records = [
        record for record in records if record.get("trace_id") == trace_id
    ]
    assert response.headers["x-trace-id"] == trace_id
    assert any(record["level"] == "ERROR" for record in request_records)
    assert any(
        record.get("event") == "http_request" and record.get("status") == 500
        for record in request_records
    )
    assert all(len(record["span_id"]) == 16 for record in request_records)


def test_scrapes_do_not_add_requests(client):
    client.get("/health")
    first = client.get("/metrics")
    second = client.get("/metrics")
    assert first.status_code == 200
    assert first.headers["content-type"].startswith("text/plain")
    assert first.text == second.text
    assert (
        sample_value(
            client,
            "http_requests_total",
            {"method": "GET", "route": "/health", "status": "200"},
        )
        == 1
    )


def test_unknown_paths_share_one_metric_label(client):
    assert client.get("/missing/one").status_code == 404
    assert client.get("/missing/two").status_code == 404
    assert (
        sample_value(
            client,
            "http_requests_total",
            {"method": "GET", "route": "unmatched", "status": "404"},
        )
        == 2
    )
    assert "/missing/one" not in client.get("/metrics").text


@pytest.mark.parametrize(
    "path",
    [
        "/slow?seconds=0",
        "/slow?seconds=4",
        "/slow?seconds=nan",
        "/load?count=0",
        "/load?count=1001",
        "/load?concurrency=0",
        "/load?concurrency=101",
    ],
)
def test_invalid_simulation_parameters_are_rejected(client, path):
    assert client.get(path).status_code == 422
