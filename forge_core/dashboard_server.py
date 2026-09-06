from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from forge_core.connectors import ConnectorRegistry
from forge_core.github_integration import (
    GitHubCredentialStore,
    GitHubIntegrationService,
)
from forge_core.integration_hub import IntegrationHub
from forge_core.loopback_api import ForgeLoopbackHTTPServer, create_loopback_server
from forge_core.release_service import ReleaseIntegrationService
from forge_core.sqlite_store import SQLiteEvidenceStore


def create_dashboard_server(
    database_path: Path,
    *,
    port: int = 43127,
    github_config_dir: Path | None = None,
) -> tuple[ForgeLoopbackHTTPServer, SQLiteEvidenceStore]:
    store = SQLiteEvidenceStore(database_path)
    try:
        service = ReleaseIntegrationService(
            store, ConnectorRegistry(), clock=lambda: datetime.now(UTC)
        )
        config_dir = (
            github_config_dir
            if github_config_dir is not None
            else database_path.parent / ".forge-local" / "github"
        )
        github_integration = GitHubIntegrationService(GitHubCredentialStore(config_dir))
        integration_hub = IntegrationHub(
            status_providers={"github": github_integration}
        )
        server = create_loopback_server(
            service,
            port=port,
            github_integration=github_integration,
            integration_hub=integration_hub,
        )
    except Exception:
        store.close()
        raise
    return server, store


def main() -> None:
    database_path = Path(os.environ.get("FORGE_DATABASE_PATH", "forge.db")).resolve()
    github_config_text = os.environ.get("FORGE_CONFIG_DIR")
    github_config_dir = (
        Path(github_config_text).expanduser().resolve() / "github"
        if github_config_text
        else None
    )
    port_text = os.environ.get("FORGE_DASHBOARD_PORT", "43127")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise SystemExit("FORGE_DASHBOARD_PORT must be an integer") from exc
    if not (0 <= port <= 65535):
        raise SystemExit("FORGE_DASHBOARD_PORT must be between 0 and 65535")

    server, store = create_dashboard_server(
        database_path, port=port, github_config_dir=github_config_dir
    )
    bound_port = int(server.server_address[1])
    print(f"FORGE dashboard: http://127.0.0.1:{bound_port}/app/", flush=True)
    print(f"Evidence store: {database_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
