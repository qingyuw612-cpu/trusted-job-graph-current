from __future__ import annotations

import sys

from new_role_discovery import demo


def test_demo_uses_resident_api_when_configured(monkeypatch, tmp_path) -> None:
    calls: list[tuple[str, str]] = []
    task_id = "task_66666666666666666666666666666666"

    def fake_api(base_url, path, *, method="GET", payload=None):
        assert base_url == "http://127.0.0.1:8070/api/v1/evolution"
        calls.append((method, path))
        if method == "POST":
            assert payload["role_review_limit"] == 50
            assert payload["skill_review_limit"] == 40
            return {"task_id": task_id, "status": "QUEUED", "progress": 0}
        if path.endswith("/result"):
            return {
                "new_role_candidates": [],
                "role_skill_changes": [],
                "manifest": {"summary": {"public_new_role_candidates": 0}},
            }
        return {
            "task_id": task_id,
            "status": "REVIEW_READY",
            "progress": 100,
            "run_id": "evolution_run:test",
            "summary": {"public_new_role_candidates": 0},
            "warnings": [],
        }

    monkeypatch.setattr(demo, "_api_json", fake_api)
    monkeypatch.setattr(
        demo, "_default_window", lambda _path: ("2026-05-01", "2026-08-30")
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "demo",
            "--neo4j-config",
            str(tmp_path / "neo4j.json"),
            "--data-root",
            str(tmp_path / "evolution"),
            "--api-url",
            "http://127.0.0.1:8070/api/v1/evolution",
        ],
    )

    assert demo.main() == 0
    assert calls == [
        ("POST", "/runs"),
        ("GET", f"/runs/{task_id}"),
        ("GET", f"/runs/{task_id}/result"),
    ]
