import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("allow_docker_host", "request_host", "expected_status"),
    [
        (False, "host.docker.internal", 421),
        (True, "host.docker.internal", 200),
        (True, "unrelated.example", 421),
        (True, "127.0.0.1", 200),
    ],
)
def test_cli_explicitly_allows_docker_host_without_disabling_host_checks(
    data, tmp_path, allow_docker_host, request_host, expected_status
):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    command = [
        sys.executable,
        "-m",
        "tender_mcp",
        "documents",
        "--data-root",
        str(data / "tenders"),
        "--port",
        str(port),
    ]
    if allow_docker_host:
        command.extend(["--allow-host", f"host.docker.internal:{port}"])
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "host-header-test", "version": "1"},
                },
            }
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Host": f"{request_host}:{port}",
        },
    )
    log_path = tmp_path / "server.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(f"MCP process exited: {log_path.read_text()}")
                try:
                    with urllib.request.urlopen(request, timeout=1) as response:
                        status, body = response.status, response.read()
                    break
                except urllib.error.HTTPError as error:
                    status, body = error.code, error.read()
                    break
                except urllib.error.URLError:
                    time.sleep(0.05)
            else:
                pytest.fail(f"MCP endpoint did not become ready: {log_path.read_text()}")
            assert status == expected_status, body
            if expected_status == 200:
                assert json.loads(body)["result"]["serverInfo"]["name"] == "tender-documents"
            else:
                assert body == b"Invalid Host header"
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
