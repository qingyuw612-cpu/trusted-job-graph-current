from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from trusted_graph_agent.radar_service import RadarRunManager


@pytest.fixture()
def manager(tmp_path: Path) -> RadarRunManager:
    project = tmp_path / "graph"
    source = tmp_path / "crawlers"
    output = tmp_path / "output"
    project.mkdir()
    (project / "config").mkdir()
    shutil.copyfile(
        Path(__file__).parents[1] / "config" / "job_radar_keywords.json",
        project / "config" / "job_radar_keywords.json",
    )
    source.mkdir()
    config = tmp_path / "neo4j.json"
    config.write_text("{}", encoding="utf-8")
    return RadarRunManager(
        project,
        config,
        python_executable="python-test",
        source_dir=source,
        output_root=output,
    )


def test_request_supports_full_multi_platform_scan(manager: RadarRunManager) -> None:
    request = manager.validate_request(
        {"platform": "all", "scan_mode": "full", "limit": 300, "pages": 2}
    )
    assert request["pages"] == 2
    assert request["keyword_count"] == 73
    assert request["platforms"] == ("51job", "zhilian", "liepin")
    assert request["limit_per_platform"] == 100
    with pytest.raises(ValueError, match="20 到 2000"):
        manager.validate_request({"platform": "zhilian", "limit": 10})


def test_command_runs_real_ingest_publish_pipeline(manager: RadarRunManager) -> None:
    request = manager.validate_request(
        {"platform": "liepin", "scan_mode": "quick", "limit": 100, "pages": 1}
    )
    command = manager.build_command(request)
    assert command[:3] == ["python-test", str(manager.project_root / "job_crawler_runner.py"), "run"]
    assert "--system-import" in command
    assert "--system-publish" in command
    assert command[command.index("--pipeline-limit") + 1] == "100"
    assert command[command.index("--collection-limit") + 1] == "100"
    assert command[command.index("--scan-mode") + 1] == "quick"
    assert command[command.index("--pages") + 1] == "1"


def test_initial_status_is_observable(manager: RadarRunManager) -> None:
    state = manager.status()
    assert state["status"] == "idle"
    assert state["progress"] == 0
    assert "等待" in state["message"]


def test_latest_discovery_result_is_sanitized(manager: RadarRunManager) -> None:
    data_root = manager.project_root / "output" / "role_evolution_workbench_v2"
    output_dir = data_root / "role_evolution_runs" / "run-1"
    jobs_dir = data_root / "role_evolution_jobs"
    output_dir.mkdir(parents=True)
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "jobs.json").write_text(
        '[{"task_id":"task_1","status":"REVIEW_READY","completed_at":"2026-08-20T12:00:00+08:00","output_dir":"'
        + str(output_dir).replace("\\", "\\\\")
        + '"}]',
        encoding="utf-8",
    )
    (output_dir / "new_role_candidates.json").write_text(
        '[{"candidate_id":"c1","candidate_title":"AI评测工程师","emergence_score":88.5,'
        '"current_jd_count":12,"current_company_count":8,"responsibility_evidence":[{"text":"private"}]}]',
        encoding="utf-8",
    )
    (output_dir / "role_skill_changes.json").write_text(
        '[{"role":"Python开发工程师","skill":"Agent开发","change_type":"INCREASED","delta":0.24}]',
        encoding="utf-8",
    )
    result = manager.latest_discovery_result()
    assert result["status"] == "ready"
    assert result["candidates"][0]["name"] == "AI评测工程师"
    assert "responsibility_evidence" not in result["candidates"][0]
    assert result["ability_changes"][0]["delta"] == 0.24


def _write_evolution_job(manager: RadarRunManager, **task) -> None:
    jobs_dir = manager.evolution_root / "role_evolution_jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "jobs.json").write_text(json.dumps([task]), encoding="utf-8")


def test_latest_evolution_result_supports_empty_running_and_failed(manager: RadarRunManager) -> None:
    assert manager.latest_evolution_result()["status"] == "empty"

    _write_evolution_job(manager, task_id="running", status="RUNNING")
    running = manager.latest_evolution_result()
    assert running["status"] == "running"
    assert running["task_id"] == "running"

    _write_evolution_job(manager, task_id="failed", status="FAILED", error="crawler error")
    failed = manager.latest_evolution_result()
    assert failed["status"] == "failed"
    assert failed["task_id"] == "failed"


def test_latest_evolution_result_is_ready_with_nullable_public_fields(manager: RadarRunManager) -> None:
    output_dir = manager.evolution_root / "role_evolution_runs" / "run-ready"
    output_dir.mkdir(parents=True)
    _write_evolution_job(
        manager,
        task_id="ready",
        status="REVIEW_READY",
        completed_at="2026-08-20T12:00:00+08:00",
        output_dir=str(output_dir),
        summary={"cutoff": "2026-07-01", "as_of": "2026-08-01"},
    )
    (output_dir / "role_skill_changes.json").write_text(
        json.dumps([
            {
                "role": "数据工程师",
                "skill": "Python",
                "change_type": "INCREASED",
                "baseline_coverage": 0.2,
                "current_coverage": 0.49,
                "delta": 0.29,
                "baseline_company_count": 12,
                "current_company_count": 31,
                "rule_state": "REVIEW",
            }
        ]),
        encoding="utf-8",
    )
    result = manager.latest_evolution_result()
    assert result["status"] == "ready"
    assert result["observation_window"] == {"cutoff": "2026-07-01", "as_of": "2026-08-01"}
    assert result["changes"][0]["delta"] == 0.29
    assert result["changes"][0]["confidence"] is None


def test_latest_evolution_result_rejects_output_outside_allowed_root(manager: RadarRunManager, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_evolution_job(manager, task_id="unsafe", status="REVIEW_READY", output_dir=str(outside))
    result = manager.latest_evolution_result()
    assert result["status"] == "failed"
    assert result["error"] == "invalid_output_dir"
    assert "outside" not in json.dumps(result)
