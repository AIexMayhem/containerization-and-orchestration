import asyncio
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp import Client

from tender_mcp.servers import create_documents_server, create_reference_server


def test_servers_expose_separate_tools_with_the_agreed_inputs(data):
    async def check():
        async with Client(create_documents_server(data / "tenders")) as client:
            result = await client.list_tools()
            tools = {tool.name: tool for tool in result.tools}
            assert set(tools) == {"list_tender_documents", "read_tender_document"}
            assert set(tools["list_tender_documents"].input_schema["required"]) == {
                "tender_id",
                "package_version",
            }
            assert set(tools["read_tender_document"].input_schema["required"]) == {
                "document_id",
                "document_version",
            }
            assert all(tool.annotations.read_only_hint for tool in tools.values())
        async with Client(create_reference_server(data / "reference")) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert set(tools) == {"get_company_profile", "get_evaluation_policy"}
            assert set(tools["get_company_profile"].input_schema["required"]) == {
                "company_id",
                "profile_version",
            }
            assert set(tools["get_evaluation_policy"].input_schema["required"]) == {
                "policy_id",
                "policy_version",
            }

    asyncio.run(check())


def test_mcp_calls_return_sources_with_citations_and_exact_versions(data):
    async def check():
        async with Client(create_documents_server(data / "tenders")) as client:
            inventory = await client.call_tool(
                "list_tender_documents", {"tender_id": "demo-tender", "package_version": "v1"}
            )
            assert inventory.is_error is False
            assert len(inventory.structured_content["documents"]) == 2
            document = await client.call_tool(
                "read_tender_document", {"document_id": "technical-spec", "document_version": "v1"}
            )
            assert document.is_error is False
            result = document.structured_content
            assert result["package_version"] == "v1"
            assert "24×7" in result["text"]
            assert any(s["source_ref"] == "technical-spec/v1#TS-2" for s in result["sections"])
        async with Client(create_reference_server(data / "reference")) as client:
            profile = await client.call_tool(
                "get_company_profile", {"company_id": "demo-integrator", "profile_version": "v1"}
            )
            policy = await client.call_tool(
                "get_evaluation_policy", {"policy_id": "tender-precheck", "policy_version": "v1"}
            )
            assert profile.is_error is False and policy.is_error is False
            assert (
                profile.structured_content["support_schedule"]["outside_schedule_available"]
                is False
            )
            assert policy.structured_content["all_package_documents_required"] is True

    asyncio.run(check())


def test_unavailable_document_becomes_mcp_tool_error(data):
    (data / "tenders/demo-tender/v1/draft-contract.md").unlink()

    async def check():
        async with Client(create_documents_server(data / "tenders")) as client:
            result = await client.call_tool(
                "read_tender_document", {"document_id": "draft-contract", "document_version": "v1"}
            )
            assert result.is_error is True
            assert "DOCUMENT_UNAVAILABLE" in " ".join(c.text for c in result.content)

    asyncio.run(check())


def test_expected_answer_and_other_source_are_not_available_as_documents(data):
    async def check():
        async with Client(create_documents_server(data / "tenders")) as client:
            for identifier in ("README", "demo-integrator"):
                result = await client.call_tool(
                    "read_tender_document", {"document_id": identifier, "document_version": "v1"}
                )
                assert result.is_error is True
                assert "UNKNOWN_DOCUMENT" in " ".join(c.text for c in result.content)

    asyncio.run(check())


@pytest.mark.parametrize(
    ("role", "directory", "expected_tools", "tool_name", "arguments", "identity_key", "identity"),
    [
        (
            "documents",
            "tenders",
            {"list_tender_documents", "read_tender_document"},
            "read_tender_document",
            {"document_id": "technical-spec", "document_version": "v1"},
            "document_id",
            "technical-spec",
        ),
        (
            "reference",
            "reference",
            {"get_company_profile", "get_evaluation_policy"},
            "get_company_profile",
            {"company_id": "demo-integrator", "profile_version": "v1"},
            "company_id",
            "demo-integrator",
        ),
    ],
)
def test_http_entrypoint_serves_the_selected_source(
    data, tmp_path, role, directory, expected_tools, tool_name, arguments, identity_key, identity
):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    log_path = tmp_path / f"{role}.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tender_mcp",
                role,
                "--data-root",
                str(data / directory),
                "--port",
                str(port),
            ],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )

        async def check():
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(f"MCP process exited: {log_path.read_text()}")
                try:
                    async with asyncio.timeout(1):
                        async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                            tools = await client.list_tools()
                            names = {tool.name for tool in tools.tools}
                            result = await client.call_tool(tool_name, arguments)
                    break
                except (AssertionError, KeyboardInterrupt):
                    raise
                except Exception:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail(f"MCP HTTP endpoint did not become ready: {log_path.read_text()}")
            assert names == expected_tools
            assert result.is_error is False
            assert result.structured_content[identity_key] == identity

        try:
            asyncio.run(check())
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
