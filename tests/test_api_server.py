from __future__ import annotations

import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from trusted_graph_agent.api_server import GraphRequestHandler
from trusted_graph_agent.radar_service import RadarRunManager


class _Repository:
    def health(self):
        return {"status": "ok"}


def test_evolution_latest_route_is_mounted(tmp_path: Path) -> None:
    project = tmp_path / "graph"
    project.mkdir()
    config = tmp_path / "neo4j.json"
    config.write_text("{}", encoding="utf-8")
    manager = RadarRunManager(project, config)
    handler = type(
        "TestGraphRequestHandler",
        (GraphRequestHandler,),
        {"repository": _Repository(), "page_path": project / "missing.html", "radar_manager": manager},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    try:
        server_port = server.server_address[1]
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection("127.0.0.1", server_port, timeout=3)
        connection.request("GET", "/api/v1/evolution/results/latest")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        assert response.status == 200
        assert payload["status"] == "empty"
    finally:
        server.shutdown()
        server.server_close()
