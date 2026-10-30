import argparse
from pathlib import Path

from mcp.server.transport_security import TransportSecuritySettings

from tender_mcp.servers import create_documents_server, create_reference_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only MCP sources for the tender experiment")
    parser.add_argument("role", choices=("documents", "reference"))
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int)
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="HOST:PORT",
        help="Additional allowed HTTP Host header; repeat to allow several names",
    )
    parser.add_argument(
        "--transport", choices=("streamable-http", "sse", "stdio"), default="streamable-http"
    )
    arguments = parser.parse_args()
    if not arguments.data_root.is_dir():
        parser.error("--data-root must point to an existing source directory")
    if arguments.port is not None and not 1 <= arguments.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if arguments.role == "documents":
        server = create_documents_server(arguments.data_root)
        default_port = 8001
    else:
        server = create_reference_server(arguments.data_root)
        default_port = 8002
    security = None
    if arguments.allow_host:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *arguments.allow_host],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        )
    if arguments.transport == "stdio":
        server.run(transport="stdio")
    elif arguments.transport == "sse":
        server.run(
            transport="sse",
            host=arguments.host,
            port=arguments.port or default_port,
            transport_security=security,
        )
    else:
        server.run(
            transport="streamable-http",
            host=arguments.host,
            port=arguments.port or default_port,
            json_response=True,
            stateless_http=True,
            transport_security=security,
        )


if __name__ == "__main__":
    main()
