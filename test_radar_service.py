from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from job_crawler_runner import parse_args as parse_crawler_args
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


def test_configuration_exposes_operating_policy_and_dynamic_limits(manager: RadarRunManager) -> None:
    config = manager.configuration()
    assert config["product"] == "猎脉 TalentGraph"
    assert config["defaults"]["scan_mode"] == "quick"
    assert config["quality_policy"]["keep_raw_snapshot"] is True
    assert config["limits"]["max_jd"] == 2000


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


def test_production_commands_share_the_system_maintenance_lock(manager: RadarRunManager, monkeypatch) -> None:
    monkeypatch.setenv("TG_MAINTENANCE_LOCK", "/var/lib/talentgraph/maintenance.lock")
    request = manager.validate_request({"scan_mode": "existing"})

    command = manager.build_command(request)

    assert command[:3] == ["/usr/bin/flock", "-n", "/var/lib/talentgraph/maintenance.lock"]
    assert command[3:7] == ["python-test", "-u", "-m", "new_role_discovery.demo"]


def test_existing_data_mode_skips_crawlers_and_runs_discovery_only(manager: RadarRunManager) -> None:
    request = manager.validate_request({"scan_mode": "existing"})
    assert request["scan_mode_label"] == "仅分析已有数据"
    assert request["platforms"] == ()
    command = manager.build_command(request)
    assert command[:4] == ["python-test", "-u", "-m", "new_role_discovery.demo"]
    assert "job_crawler_runner.py" not in command
    assert "--skill-limit" in command
    assert command[command.index("--skill-limit") + 1] == "5"


def test_demo_mode_is_bounded_and_tolerates_single_llm_errors(manager: RadarRunManager) -> None:
    request = manager.validate_request({"platform": "all", "scan_mode": "demo", "limit": 10, "pages": 1})
    assert request["platforms"] == ("zhilian",)
    assert request["limit"] == 10
    command = manager.build_command(request)
    assert "--platform" in command and command[command.index("--platform") + 1] == "zhilian"
    assert command[command.index("--collection-limit") + 1] == "10"
    assert "--allow-processing-failures" in command
    assert "--system-publish" in command
    assert "--fast-demo" not in command
    assert command[command.index("--new-role-limit") + 1] == "5"
    assert command[command.index("--ability-change-limit") + 1] == "5"


def test_demo_command_is_accepted_by_the_bundled_crawler_runner(manager: RadarRunManager) -> None:
    """Keep the radar service and crawler CLI deployable as one compatible unit."""
    request = manager.validate_request(
        {"platform": "all", "scan_mode": "demo", "limit": 200, "pages": 1}
    )
    command = manager.build_command(request)

    args = parse_crawler_args(command[2:])

    assert args.command == "run"
    assert args.fast_demo is False
    assert args.system_publish is True
    assert args.allow_processing_failures is True
    assert args.new_role_limit == 5
    assert args.ability_change_limit == 5


def test_demo_mode_clamps_unsafe_limits(manager: RadarRunManager) -> None:
    too_large = manager.validate_request(
        {"platform": "all", "scan_mode": "demo", "limit": 9999, "pages": 3}
    )
    too_small = manager.validate_request(
        {"platform": "all", "scan_mode": "demo", "limit": 0, "pages": 1}
    )
    assert too_large["limit"] == 200
    assert too_large["limit_per_platform"] == 200
    assert too_small["limit"] == 1
    assert too_small["limit_per_platform"] == 1


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
        '"current_jd_count":12,"current_company_count":8,"responsibility_evidence":[{"text":"private"}],'
        '"semantic_review":{"status":"COMPLETED","analysis":{"semantic_class":"NEW_ROLE",'
        '"canonical_name":"AI评测工程师","role_boundary":"负责大模型系统化评测与质量治理",'
        '"confidence":0.91}}}]',
        encoding="utf-8",
    )
    (output_dir / "role_skill_changes.json").write_text(
        '[{"role":"Python开发工程师","skill":"Agent开发","change_type":"INCREASED","delta":0.24}]',
        encoding="utf-8",
    )
    result = manager.latest_discovery_result()
    assert result["status"] == "ready"
    assert result["candidates"][0]["name"] == "AI评测工程师"
    assert result["candidates"][0]["status"] == "AI已提供辅助建议，待人工审核"
    assert "responsibility_evidence" not in result["candidates"][0]
    assert result["ability_changes"][0]["delta"] == 0.24


def test_latest_discovery_exposes_strict_candidates_without_completed_ai_review(
    manager: RadarRunManager,
) -> None:
    output_dir = manager.evolution_root / "role_evolution_runs" / "run-unreviewed"
    output_dir.mkdir(parents=True)
    _write_evolution_job(
        manager,
        task_id="unreviewed",
        status="REVIEW_READY",
        completed_at="2026-08-20T12:00:00+08:00",
        output_dir=str(output_dir),
    )
    (output_dir / "new_role_candidates.json").write_text(
        json.dumps(
            [
                {
                    "candidate_id": "rule-only",
                        "candidate_title": "规则候选",
                        "rule_state": "WATCH",
                        "publication_state": "PUBLISHED_CANDIDATE",
                },
                {
                    "candidate_id": "alias",
                    "candidate_title": "别名候选",
                    "semantic_review": {
                        "status": "COMPLETED",
                        "analysis": {
                            "semantic_class": "ALIAS",
                            "canonical_name": "已有岗位",
                            "role_boundary": "属于已有岗位别名",
                        },
                    },
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (output_dir / "role_skill_changes.json").write_text("[]", encoding="utf-8")

    result = manager.latest_discovery_result()

    assert result["status"] == "ready"
    assert [item["id"] for item in result["candidates"]] == ["rule-only"]
    assert "待人工审核" in result["candidates"][0]["status"]


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
