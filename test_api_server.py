from __future__ import annotations

import json
from datetime import datetime
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from trusted_graph_agent.api_server import GraphRequestHandler, MaintenanceStatusReader
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


def test_maintenance_status_reader_returns_public_snapshot(tmp_path: Path) -> None:
    output_root = tmp_path / "crawler"
    output_root.mkdir()
    (output_root / "current_status.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "success",
                "phase": "complete",
                "message": "本轮维护已完成",
                "progress_percent": 100,
                "platforms": [{"platform": "51job", "rows_added": 12}],
                "totals": {"rows_added": 12},
                "last_success_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "schedule": {"interval_hours": 12},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = MaintenanceStatusReader(output_root).status()

    assert payload["status"] == "success"
    assert payload["totals"]["rows_added"] == 12
    assert payload["freshness"]["state"] == "fresh"
    assert payload["freshness"]["closed_loop_healthy"] is True
    assert "fetched_at" in payload


def test_maintenance_status_reader_falls_back_to_latest_manifest(tmp_path: Path) -> None:
    output_root = tmp_path / "crawler"
    run_dir = output_root / "runs" / "cycle-1"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "cycle_id": "cycle-1",
                "status": "partial_failure",
                "started_at": "2026-08-28T01:00:00+08:00",
                "finished_at": "2026-08-28T02:00:00+08:00",
                "platforms": [
                    {
                        "platform": "51job",
                        "label": "前程无忧",
                        "status": "success",
                        "rows_added": 1127,
                        "rows_selected_for_import": 1000,
                    },
                    {
                        "platform": "liepin",
                        "label": "猎聘",
                        "status": "failed",
                        "rows_added": 0,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = MaintenanceStatusReader(output_root).status()

    assert payload["status"] == "partial_failure"
    assert payload["schema_version"] == 2
    assert payload["freshness"]["state"] == "degraded"
    assert payload["freshness"]["closed_loop_healthy"] is False
    assert payload["totals"]["rows_added"] == 1127
    assert payload["platforms"][1]["status"] == "failed"
    assert "command" not in json.dumps(payload, ensure_ascii=False)


def test_successful_legacy_manifest_is_counted_as_last_success(tmp_path: Path) -> None:
    output_root = tmp_path / "crawler"
    run_dir = output_root / "runs" / "cycle-success"
    run_dir.mkdir(parents=True)
    finished = datetime.now().astimezone().isoformat(timespec="seconds")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "cycle_id": "cycle-success",
                "status": "success",
                "started_at": finished,
                "finished_at": finished,
                "platforms": [],
                "graph_updates": {"publish_status": "COMPLETED"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = MaintenanceStatusReader(output_root).status()

    assert payload["schema_version"] == 2
    assert payload["last_success_cycle_id"] == "cycle-success"
    assert payload["freshness"]["state"] == "fresh"
    assert payload["freshness"]["closed_loop_healthy"] is True


def test_partial_sources_with_successful_publish_keep_closed_loop_healthy(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "crawler"
    output_root.mkdir()
    finished = datetime.now().astimezone().isoformat(timespec="seconds")
    (output_root / "current_status.json").write_text(
        json.dumps(
            {
                "status": "partial_failure",
                "phase": "complete",
                "cycle_id": "partial-published",
                "finished_at": finished,
                "graph_updates": {"publish_status": "COMPLETED"},
                "schedule": {"interval_hours": 12},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = MaintenanceStatusReader(output_root).status()

    assert payload["last_success_cycle_id"] == "partial-published"
    assert payload["freshness"]["state"] == "fresh_with_warnings"
    assert payload["freshness"]["closed_loop_healthy"] is True


def test_maintenance_status_route_is_mounted(tmp_path: Path) -> None:
    output_root = tmp_path / "crawler"
    handler = type(
        "TestMaintenanceRequestHandler",
        (GraphRequestHandler,),
        {
            "repository": _Repository(),
            "page_path": tmp_path / "missing.html",
            "maintenance_reader": MaintenanceStatusReader(output_root),
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    try:
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
        connection.request("GET", "/api/v1/maintenance/status")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        assert response.status == 200
        assert payload["status"] == "idle"
        assert payload["schedule"]["interval_hours"] == 12
    finally:
        server.shutdown()
        server.server_close()
