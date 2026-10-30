from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from tender_mcp.sources import DocumentSource, ReferenceSource, SourceError

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def _call_source(operation: Callable[..., dict], *arguments: str) -> dict[str, Any]:
    try:
        return operation(*arguments)
    except SourceError as exc:
        raise ToolError(str(exc)) from exc


def create_documents_server(root: Path) -> MCPServer:
    source = DocumentSource(root)
    server = MCPServer("tender-documents", version="0.1.0")

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def list_tender_documents(tender_id: str, package_version: str) -> dict[str, Any]:
        """Получить полный состав выбранной версии пакета и обязательность документов."""
        return _call_source(source.list_tender_documents, tender_id, package_version)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def read_tender_document(document_id: str, document_version: str) -> dict[str, Any]:
        """Прочитать точную версию документа: текст, разделы и ссылки на источник.

        Сначала получить идентификаторы из list_tender_documents. Ошибка чтения
        означает невозможность проверки, а не отсутствие требования в документе.
        """
        return _call_source(source.read_tender_document, document_id, document_version)

    return server


def create_reference_server(root: Path) -> MCPServer:
    source = ReferenceSource(root)
    server = MCPServer("company-reference", version="0.1.0")

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_company_profile(company_id: str, profile_version: str) -> dict[str, Any]:
        """Получить точную версию возможностей и ограничений компании."""
        return _call_source(source.get_company_profile, company_id, profile_version)

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_evaluation_policy(policy_id: str, policy_version: str) -> dict[str, Any]:
        """Получить точную версию критериев и правил оценки тендерной документации."""
        return _call_source(source.get_evaluation_policy, policy_id, policy_version)

    return server
