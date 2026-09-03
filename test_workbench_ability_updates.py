from __future__ import annotations

import json
from pathlib import Path

from new_role_discovery.workbench import EvolutionWorkbenchService


def test_latest_skill_changes_skips_newer_title_only_run(tmp_path: Path) -> None:
    data_root = tmp_path / "evolution"
    runs_root = data_root / "role_evolution_runs"
    jobs_root = data_root / "role_evolution_jobs"
    older_run = runs_root / "older"
    latest_run = runs_root / "latest"
    older_run.mkdir(parents=True)
    latest_run.mkdir(parents=True)
    jobs_root.mkdir(parents=True)

    (older_run / "role_skill_changes.json").write_text(
        json.dumps(
            [
                {
                    "role": "Python开发工程师",
                    "skill": "Agent开发",
                    "change_type": "INCREASED",
                    "delta": 0.24,
                },
                {
                    "role": "",
                    "skill": "内部审计占位",
                    "change_type": "INCREASED",
                    "delta": 0.99,
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (latest_run / "role_skill_changes.json").write_text("[]", encoding="utf-8")
    older_id = "task_11111111111111111111111111111111"
    latest_id = "task_22222222222222222222222222222222"
    (jobs_root / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "task_id": older_id,
                    "status": "DEGRADED_REVIEW_READY",
                    "completed_at": "2026-08-29T18:37:59+08:00",
                    "output_dir": str(older_run),
                },
                {
                    "task_id": latest_id,
                    "status": "REVIEW_READY",
                    "completed_at": "2026-08-29T20:08:43+08:00",
                    "output_dir": str(latest_run),
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    service = EvolutionWorkbenchService(tmp_path / "neo4j.json", data_root)
    try:
        result = service.get_latest_skill_changes()
    finally:
        service.close()

    assert result["status"] == "ready"
    assert result["task"]["task_id"] == older_id
    assert result["latest_task_id"] == latest_id
    assert result["is_historical_snapshot"] is True
    assert result["role_skill_changes"][0]["skill"] == "Agent开发"
    assert result["algorithm_version"] == "real-title-discovery-v1.1"
    assert result["result_schema_version"] == "new-role-result-v1"
    assert result["ability_result_summary"] == {
        "role_count": 1,
        "change_count": 1,
        "reviewed_role_count": 0,
        "review_task_count": 0,
    }


def test_latest_public_run_keeps_full_snapshot_when_increment_is_small(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "evolution"
    runs_root = data_root / "role_evolution_runs"
    jobs_root = data_root / "role_evolution_jobs"
    full_run = runs_root / "full"
    incremental_run = runs_root / "incremental"
    full_run.mkdir(parents=True)
    incremental_run.mkdir(parents=True)
    jobs_root.mkdir(parents=True)

    def candidates(count: int) -> list[dict[str, str]]:
        return [
            {
                "candidate_id": f"candidate-{count}-{index}",
                "candidate_title": f"新岗位{index}",
                "publication_state": "PUBLISHED_CANDIDATE",
            }
            for index in range(count)
        ]

    (full_run / "new_role_candidates.json").write_text(
        json.dumps(candidates(10), ensure_ascii=False), encoding="utf-8"
    )
    (incremental_run / "new_role_candidates.json").write_text(
        json.dumps(candidates(3), ensure_ascii=False), encoding="utf-8"
    )
    full_id = "task_44444444444444444444444444444444"
    incremental_id = "task_55555555555555555555555555555555"
    (jobs_root / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "task_id": full_id,
                    "status": "DEGRADED_REVIEW_READY",
                    "completed_at": "2026-08-30T18:00:00+08:00",
                    "output_dir": str(full_run),
                },
                {
                    "task_id": incremental_id,
                    "status": "DEGRADED_REVIEW_READY",
                    "completed_at": "2026-08-30T23:47:20+08:00",
                    "output_dir": str(incremental_run),
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    service = EvolutionWorkbenchService(tmp_path / "neo4j.json", data_root)
    try:
        result = service.get_latest_public_run()
    finally:
        service.close()

    assert result["status"] == "ready"
    assert result["task"]["task_id"] == full_id
    assert result["latest_task_id"] == incremental_id
    assert result["public_candidate_count"] == 10
    assert result["public_snapshot_minimum"] == 8
    assert result["is_historical_snapshot"] is True
    assert result["source_policy"] == "LATEST_EVIDENCE_COMPLETE_PUBLIC_SNAPSHOT"


def test_result_window_keeps_all_public_candidates_visible(tmp_path: Path) -> None:
    data_root = tmp_path / "evolution"
    run_dir = data_root / "role_evolution_runs" / "run"
    jobs_root = data_root / "role_evolution_jobs"
    run_dir.mkdir(parents=True)
    jobs_root.mkdir(parents=True)
    task_id = "task_33333333333333333333333333333333"
    candidates = [
        {
            "candidate_id": f"candidate-{index}",
            "candidate_title": f"观察候选{index}",
            "publication_state": "OBSERVATION_ONLY",
        }
        for index in range(50)
    ]
    candidates.extend(
        {
            "candidate_id": f"public-{index}",
            "candidate_title": f"新岗位{index}",
            "publication_state": "PUBLISHED_CANDIDATE",
        }
        for index in range(10)
    )
    (run_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "algorithm_version": "real-title-discovery-v1.0",
                "result_schema_version": "new-role-result-v1",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "new_role_candidates.json").write_text(
        json.dumps(candidates, ensure_ascii=False), encoding="utf-8"
    )
    (jobs_root / "jobs.json").write_text(
        json.dumps(
            [
                {
                    "task_id": task_id,
                    "status": "REVIEW_READY",
                    "output_dir": str(run_dir),
                    "summary": {
                        "algorithm_version": "real-title-discovery-v1.0",
                        "result_schema_version": "new-role-result-v1",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    service = EvolutionWorkbenchService(tmp_path / "neo4j.json", data_root)
    try:
        result = service.get_result(task_id)
    finally:
        service.close()

    visible = [
        item
        for item in result["new_role_candidates"]
        if item["publication_state"] == "PUBLISHED_CANDIDATE"
    ]
    assert len(visible) == 10
    assert result["candidate_result_window"] == {
        "returned": 50,
        "total": 60,
        "full_artifact": "new_role_candidates.json",
    }
