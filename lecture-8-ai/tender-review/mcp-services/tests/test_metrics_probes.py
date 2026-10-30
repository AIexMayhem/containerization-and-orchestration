import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from tender_mcp.metrics_probes import (
    ProbeSettings,
    build_probes,
    probe_documents,
    probe_n8n,
    probe_ollama,
    probe_reference,
)
from tender_mcp.servers import create_documents_server, create_reference_server


def documents_server(payload=None, *, include_reader=True, delay=0, error=False):
    server = MCPServer("probe-test-documents")

    @server.tool(structured_output=True)
    async def list_tender_documents(tender_id: str, package_version: str) -> dict[str, Any]:
        await asyncio.sleep(delay)
        if error:
            raise ToolError("PACKAGE_UNAVAILABLE")
        return payload

    if include_reader:

        @server.tool(structured_output=True)
        def read_tender_document(document_id: str, document_version: str) -> dict[str, Any]:
            raise ToolError("Readiness must not read the whole package")

    return server


def reference_server(payload, *, include_policy=True):
    server = MCPServer("probe-test-reference")

    @server.tool(structured_output=True)
    def get_company_profile(company_id: str, profile_version: str) -> dict[str, Any]:
        return payload

    if include_policy:

        @server.tool(structured_output=True)
        def get_evaluation_policy(policy_id: str, policy_version: str) -> dict[str, Any]:
            raise ToolError("Readiness need not fetch the policy")

    return server


@contextmanager
def http_server(status=200, body=b"{}", delay=0):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            time.sleep(delay)
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@contextmanager
def streaming_http_server():
    started = threading.Event()
    disconnected = threading.Event()
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.connection.settimeout(0.01)
            started.set()
            deadline = time.monotonic() + 0.8
            while not stop.is_set() and time.monotonic() < deadline:
                try:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    if self.connection.recv(1, socket.MSG_PEEK) == b"":
                        disconnected.set()
                        break
                except TimeoutError:
                    continue
                except OSError:
                    disconnected.set()
                    break

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", started, disconnected
    finally:
        stop.set()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


async def wait_for_event(event, timeout=0.3):
    async with asyncio.timeout(timeout):
        while not event.is_set():
            await asyncio.sleep(0.005)


def test_document_readiness_checks_protocol_and_inventory_without_reading_files(data):
    (data / "tenders/demo-tender/v1/draft-contract.md").unlink()
    server = create_documents_server(data / "tenders")
    assert asyncio.run(probe_documents(server, "demo-tender", "v1")) is True
    assert asyncio.run(probe_documents(server, "unknown-tender", "v1")) is False


def test_reference_readiness_checks_protocol_and_profile(data):
    server = create_reference_server(data / "reference")
    assert asyncio.run(probe_reference(server, "demo-integrator", "v1")) is True
    assert asyncio.run(probe_reference(server, "unknown-company", "v1")) is False


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "tender_id": "wrong",
            "package_version": "v1",
            "documents": [{"document_id": "a", "document_version": "v1", "required": True}],
        },
        {
            "tender_id": "demo-tender",
            "package_version": "v2",
            "documents": [{"document_id": "a", "document_version": "v1", "required": True}],
        },
        {"tender_id": "demo-tender", "package_version": "v1", "documents": "bad"},
        {"tender_id": "demo-tender", "package_version": "v1", "documents": []},
        {"tender_id": "demo-tender", "package_version": "v1", "documents": [{}]},
        {
            "tender_id": "demo-tender",
            "package_version": "v1",
            "documents": [{"document_id": "a", "document_version": "v1", "required": "yes"}],
        },
        {
            "tender_id": "demo-tender",
            "package_version": "v1",
            "documents": [{"document_id": "", "document_version": "v1", "required": True}],
        },
    ],
)
def test_document_readiness_rejects_invalid_inventory(payload):
    assert asyncio.run(probe_documents(documents_server(payload), "demo-tender", "v1")) is False


def test_document_readiness_requires_both_tools_and_successful_tool_result():
    payload = {
        "tender_id": "demo-tender",
        "package_version": "v1",
        "documents": [{"document_id": "a", "document_version": "v1", "required": True}],
    }
    assert asyncio.run(probe_documents(documents_server(payload), "demo-tender", "v1")) is True
    assert (
        asyncio.run(
            probe_documents(documents_server(payload, include_reader=False), "demo-tender", "v1")
        )
        is False
    )
    assert asyncio.run(probe_documents(documents_server(error=True), "demo-tender", "v1")) is False


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"company_id": "wrong", "profile_version": "v1", "support_schedule": {}},
        {"company_id": "demo-integrator", "profile_version": "v2", "support_schedule": {}},
        {"company_id": "demo-integrator", "profile_version": "v1", "support_schedule": []},
    ],
)
def test_reference_readiness_rejects_wrong_profile_or_schedule_type(payload):
    assert asyncio.run(probe_reference(reference_server(payload), "demo-integrator", "v1")) is False


def test_reference_readiness_requires_policy_tool():
    payload = {"company_id": "demo-integrator", "profile_version": "v1", "support_schedule": {}}
    assert (
        asyncio.run(
            probe_reference(
                reference_server(payload, include_policy=False), "demo-integrator", "v1"
            )
        )
        is False
    )


def test_mcp_probe_times_out_and_preserves_cancellation():
    async def check():
        server = documents_server(delay=5)
        async with asyncio.timeout(1):
            assert await probe_documents(server, "demo-tender", "v1", timeout=0.05) is False
        task = asyncio.create_task(probe_documents(server, "demo-tender", "v1", timeout=5))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(check())


@pytest.mark.parametrize("status,expected", [(200, True), (302, False), (404, False), (503, False)])
def test_n8n_uses_readiness_endpoint(status, expected):
    with http_server(status=status) as (url, requests):
        assert asyncio.run(probe_n8n(url + "/")) is expected
    assert requests == ["/healthz/readiness"]


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, b'{"models": [{"name": "qwen3:8b"}]}', True),
        (200, b'{"models": [{"name": "other:8b"}]}', False),
        (200, b'{"models": []}', False),
        (200, b'{"models": ["qwen3:8b"]}', False),
        (200, b'{"models": {"name": "qwen3:8b"}}', False),
        (200, b"[]", False),
        (200, b"broken json", False),
        (503, b'{"models": [{"name": "qwen3:8b"}]}', False),
    ],
)
def test_ollama_only_lists_models_and_requires_installed_model(status, body, expected):
    with http_server(status=status, body=body) as (url, requests):
        assert asyncio.run(probe_ollama(url, "qwen3:8b")) is expected
    assert requests == ["/api/tags"]


def test_http_probes_report_unavailable_port_without_raising():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        assert asyncio.run(probe_n8n(url, timeout=0.1)) is False
        assert asyncio.run(probe_ollama(url, "qwen3:8b", timeout=0.1)) is False


def test_http_probes_bound_wait_for_a_slow_dependency():
    async def check(url):
        async with asyncio.timeout(0.2):
            assert await probe_n8n(url, timeout=0.03) is False
            assert await probe_ollama(url, "qwen3:8b", timeout=0.03) is False

    with http_server(delay=0.3) as (url, _requests):
        asyncio.run(check(url))


@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancellation"])
def test_ollama_probe_closes_streaming_connection_when_interrupted(cancel):
    async def check(url, started, disconnected):
        task = asyncio.create_task(probe_ollama(url, "qwen3:8b", timeout=1 if cancel else 0.1))
        await wait_for_event(started)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task is False
        await wait_for_event(disconnected)

    with streaming_http_server() as (url, started, disconnected):
        asyncio.run(check(url, started, disconnected))


def test_n8n_probe_closes_streaming_body_after_reading_status():
    async def check(url, started, disconnected):
        assert await probe_n8n(url, timeout=0.1) is True
        await wait_for_event(started)
        await wait_for_event(disconnected)

    with streaming_http_server() as (url, started, disconnected):
        asyncio.run(check(url, started, disconnected))


def test_ollama_probe_rejects_oversized_model_inventory():
    body = json.dumps({"models": [{"name": "qwen3:8b"}], "padding": "x" * 1_048_576}).encode()
    with http_server(body=body) as (url, _requests):
        assert asyncio.run(probe_ollama(url, "qwen3:8b")) is False


def test_probe_builder_wires_selected_model_and_service_urls():
    with http_server(body=json.dumps({"models": [{"name": "custom:4b"}]}).encode()) as (
        url,
        requests,
    ):
        probes = build_probes(ProbeSettings(n8n_url=url, ollama_url=url, model="custom:4b"))
        assert set(probes) == {"documents", "reference", "n8n", "ollama"}
        assert asyncio.run(probes["n8n"]()) is True
        assert asyncio.run(probes["ollama"]()) is True
    assert requests == ["/healthz/readiness", "/api/tags"]
