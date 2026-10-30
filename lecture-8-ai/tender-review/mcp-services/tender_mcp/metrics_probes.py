"""Bounded read-only dependency checks; readiness does not verify a full tender package."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial

from httpx2 import AsyncClient
from mcp import Client
from mcp.server import MCPServer


@dataclass(frozen=True)
class ProbeSettings:
    documents_url: str = "http://127.0.0.1:8001/mcp"
    reference_url: str = "http://127.0.0.1:8002/mcp"
    n8n_url: str = "http://127.0.0.1:5678"
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:8b"
    tender_id: str = "demo-tender"
    package_version: str = "v1"
    company_id: str = "demo-integrator"
    profile_version: str = "v1"
    timeout: float = 5.0


async def _probe_mcp(
    endpoint: str | MCPServer,
    required_tools: set[str],
    tool_name: str,
    arguments: dict[str, str],
    valid_response: Callable[[dict], bool],
    timeout: float,
) -> bool:
    try:
        async with asyncio.timeout(timeout):
            async with Client(endpoint) as client:
                tools = await client.list_tools()
                if not required_tools.issubset({tool.name for tool in tools.tools}):
                    return False
                result = await client.call_tool(tool_name, arguments)
                return (
                    not result.is_error
                    and isinstance(result.structured_content, dict)
                    and valid_response(result.structured_content)
                )
    except Exception:
        return False


def _valid_inventory(payload: dict, tender_id: str, package_version: str) -> bool:
    documents = payload.get("documents")
    if (
        payload.get("tender_id") != tender_id
        or payload.get("package_version") != package_version
        or not isinstance(documents, list)
        or not documents
    ):
        return False
    identities = set()
    for document in documents:
        if not isinstance(document, dict):
            return False
        identity = (document.get("document_id"), document.get("document_version"))
        if (
            not all(isinstance(value, str) and value.strip() for value in identity)
            or not isinstance(document.get("required"), bool)
            or identity in identities
        ):
            return False
        identities.add(identity)
    return True


async def probe_documents(
    endpoint: str | MCPServer, tender_id: str, package_version: str, timeout: float = 5.0
) -> bool:
    return await _probe_mcp(
        endpoint,
        {"list_tender_documents", "read_tender_document"},
        "list_tender_documents",
        {"tender_id": tender_id, "package_version": package_version},
        lambda payload: _valid_inventory(payload, tender_id, package_version),
        timeout,
    )


async def probe_reference(
    endpoint: str | MCPServer, company_id: str, profile_version: str, timeout: float = 5.0
) -> bool:
    return await _probe_mcp(
        endpoint,
        {"get_company_profile", "get_evaluation_policy"},
        "get_company_profile",
        {"company_id": company_id, "profile_version": profile_version},
        lambda payload: (
            payload.get("company_id") == company_id
            and payload.get("profile_version") == profile_version
            and isinstance(payload.get("support_schedule"), dict)
        ),
        timeout,
    )


async def probe_n8n(url: str, timeout: float = 5.0) -> bool:
    try:
        async with asyncio.timeout(timeout):
            async with AsyncClient(
                trust_env=False, follow_redirects=False, timeout=timeout
            ) as client:
                async with client.stream("GET", url.rstrip("/") + "/healthz/readiness") as response:
                    return response.status_code == 200
    except Exception:
        return False


async def probe_ollama(url: str, model: str, timeout: float = 5.0) -> bool:
    try:
        async with asyncio.timeout(timeout):
            async with AsyncClient(
                trust_env=False, follow_redirects=False, timeout=timeout
            ) as client:
                async with client.stream("GET", url.rstrip("/") + "/api/tags") as response:
                    if response.status_code != 200:
                        return False
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 1_048_576:
                            return False
            payload = json.loads(body)
            models = payload.get("models") if isinstance(payload, dict) else None
            return isinstance(models, list) and any(
                isinstance(entry, dict) and entry.get("name") == model for entry in models
            )
    except Exception:
        return False


def build_probes(settings: ProbeSettings) -> dict[str, Callable[[], Awaitable[bool]]]:
    return {
        "documents": partial(
            probe_documents,
            settings.documents_url,
            settings.tender_id,
            settings.package_version,
            timeout=settings.timeout,
        ),
        "reference": partial(
            probe_reference,
            settings.reference_url,
            settings.company_id,
            settings.profile_version,
            timeout=settings.timeout,
        ),
        "n8n": partial(probe_n8n, settings.n8n_url, timeout=settings.timeout),
        "ollama": partial(
            probe_ollama, settings.ollama_url, settings.model, timeout=settings.timeout
        ),
    }
