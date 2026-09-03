from __future__ import annotations

import json
import sys
from pathlib import Path

from job_crawler_runner import (
    PLATFORMS,
    RunOptions,
    build_command,
    build_system_finalize_command,
    build_system_ingest_command,
    acquire_lock,
    run_cycle,
    run_logged,
    scan_keywords,
    write_capped_csv,
    write_live_status,
)
import pytest


def make_options(tmp_path: Path, **overrides) -> RunOptions:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    for name in ("main_51job.py", "spider_zhilian_step1.py", "liepin_cdp_raw.py"):
        (source / name).write_text("print('unused')\n", encoding="utf-8")
    values = {
        "platforms": PLATFORMS,
        "output_root": tmp_path / "output",
        "source_dir": source,
        "year": 2026,
        "pages": 1,
        "city": "北京",
        "keyword": "产品经理",
        "fresh_scan": True,
        "non_interactive": True,
        "dry_run": True,
        "python_executable": sys.executable,
        "reuse_output": False,
        "system_import": False,
        "system_publish": False,
        "pipeline_limit": 0,
        "neo4j_config": tmp_path / "neo4j.json",
        "skip_new_role_discovery": False,
    }
    values.update(overrides)
    return RunOptions(**values)


def test_builds_bounded_commands_for_all_platforms(tmp_path):
    options = make_options(tmp_path)
    commands = {platform: build_command(platform, options)[0] for platform in PLATFORMS}

    assert commands["51job"][-4:] == ["--keyword", "产品经理", "--city", "北京"]
    assert "--reset-checkpoint" in commands["51job"]
    assert "--no-resume" in commands["zhilian"]
    assert "--target-keyword" in commands["liepin"]
    assert all("1" in command for command in commands.values())


def test_dry_run_writes_a_non_importing_manifest(tmp_path):
    options = make_options(tmp_path)
    code, manifest_path = run_cycle(options)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert code == 0
    assert manifest["status"] == "planned"
    assert manifest["system_import_enabled"] is False
    assert [item["platform"] for item in manifest["platforms"]] == list(PLATFORMS)
    assert all(item["status"] == "planned" for item in manifest["platforms"])
    status = json.loads(
        (options.output_root / "current_status.json").read_text(encoding="utf-8")
    )
    assert status["status"] == "planned"
    assert status["phase"] == "complete"
    assert status["progress_percent"] == 100
    assert "command" not in json.dumps(status, ensure_ascii=False)


def test_live_status_preserves_last_success_across_next_running_cycle(tmp_path):
    output = tmp_path / "output"
    write_live_status(
        output,
        {
            "status": "success",
            "phase": "complete",
            "cycle_id": "cycle-ok",
            "graph_updates": {"publish_status": "COMPLETED"},
        },
    )
    first = json.loads((output / "current_status.json").read_text(encoding="utf-8"))
    assert first["schema_version"] == 2
    assert first["last_success_cycle_id"] == "cycle-ok"
    assert first["last_success_at"]

    write_live_status(
        output,
        {"status": "running", "phase": "collect", "cycle_id": "cycle-next"},
    )
    second = json.loads((output / "current_status.json").read_text(encoding="utf-8"))
    assert second["last_success_cycle_id"] == "cycle-ok"
    assert second["last_success_at"] == first["last_success_at"]


def test_partial_source_failure_still_records_successful_graph_publish(tmp_path):
    output = tmp_path / "output"
    write_live_status(
        output,
        {
            "status": "partial_failure",
            "phase": "complete",
            "cycle_id": "cycle-partial",
            "graph_updates": {"publish_status": "COMPLETED"},
        },
    )
    payload = json.loads(
        (output / "current_status.json").read_text(encoding="utf-8")
    )
    assert payload["last_success_cycle_id"] == "cycle-partial"
    assert payload["last_success_at"]


def test_system_handoff_uses_guarded_partial_import_then_one_publish(tmp_path):
    options = make_options(tmp_path, system_import=True, system_publish=True)
    output = tmp_path / "jobs.csv"
    ingest = build_system_ingest_command("liepin", output, options)
    finalize = build_system_finalize_command(options)

    assert str(output) in ingest
    assert ingest[ingest.index("--platform") + 1] == "猎聘"
    assert "--skip-normalization" in ingest
    assert "--iflytek-spark" in ingest
    assert "--force-import" in ingest
    assert "--publish" not in ingest
    assert "--skip-import" in finalize
    assert "--publish" in finalize


def test_finalize_forwards_all_unique_ingestion_runs_to_incremental_normalizer(tmp_path):
    options = make_options(tmp_path, system_import=True, system_publish=True)
    finalize = build_system_finalize_command(
        options, ingest_run_ids=("rawrun:a", "rawrun:b", "rawrun:a")
    )
    values = [
        finalize[index + 1]
        for index, value in enumerate(finalize[:-1])
        if value == "--ingest-run-id"
    ]
    assert values == ["rawrun:a", "rawrun:b"]


def test_finalize_uses_shared_evolution_data_root(tmp_path, monkeypatch):
    shared = tmp_path / "persistent-evolution"
    monkeypatch.setenv("EVOLUTION_DATA_ROOT", str(shared))
    options = make_options(tmp_path, system_import=True, system_publish=True)

    finalize = build_system_finalize_command(options)

    assert finalize[finalize.index("--new-role-data-root") + 1] == str(shared)


def test_finalize_reuses_both_evolution_branches_with_expanded_ability_budget(
    tmp_path,
):
    options = make_options(tmp_path, system_import=True, system_publish=True)

    finalize = build_system_finalize_command(options)

    assert finalize[finalize.index("--new-role-limit") + 1] == "50"
    assert finalize[finalize.index("--ability-change-limit") + 1] == "40"


def test_fast_demo_finalize_uses_optional_llm_credentials(tmp_path):
    options = make_options(tmp_path, system_import=True, fast_demo=True)

    finalize = build_system_finalize_command(options)

    assert finalize[finalize.index("--llm-mode") + 1] == "auto"
    assert "--sample-limit" in finalize


def test_fast_demo_without_new_rows_reuses_existing_graph(tmp_path, monkeypatch):
    options = make_options(
        tmp_path,
        platforms=("zhilian",),
        dry_run=False,
        system_import=True,
        fast_demo=True,
        collection_limit=1,
        scan_mode="target",
    )
    monkeypatch.delenv("IFLYTEK_SPARK_API_PASSWORD", raising=False)

    def fake_run_logged(command, log_path, cwd, append=False):
        del cwd, append
        if "spider_zhilian_step1.py" in " ".join(command):
            output = Path(command[command.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("jobID,职位名称\n", encoding="utf-8")
        log_path.write_text("ok\n", encoding="utf-8")
        return 0, 0.01

    monkeypatch.setattr("job_crawler_runner.run_logged", fake_run_logged)

    code, manifest_path = run_cycle(options)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert code == 0
    assert manifest["status"] == "success"
    assert manifest["system_integration"]["imports"][0]["status"] == "skipped"
    assert "没有新增 JD" in manifest["system_integration"]["imports"][0]["reason"]
    assert manifest["system_integration"]["finalize"]["status"] == "skipped"
    assert "保留当前活动图谱" in manifest["system_integration"]["finalize"]["reason"]


def test_one_platform_failure_does_not_block_successful_platform_import(tmp_path, monkeypatch):
    options = make_options(
        tmp_path,
        platforms=("51job", "liepin"),
        dry_run=False,
        system_import=True,
        collection_limit=1,
        scan_mode="target",
        non_interactive=False,
    )

    def fake_run_logged(command, log_path, cwd, append=False):
        del cwd, append
        joined = " ".join(command)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("status only\n", encoding="utf-8")
        if "main_51job.py" in joined:
            output_dir = Path(command[command.index("--output-dir") + 1])
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "jobs_2026_it.csv").write_text(
                "jobID,职位名称\n1,测试岗位\n", encoding="utf-8"
            )
            return 0, 0.01
        if "liepin_cdp_raw.py" in joined:
            return 1, 0.01
        if "run_incremental_knowledge_graph.py" in joined:
            return 1, 0.01
        return 0, 0.01

    monkeypatch.setattr("job_crawler_runner.run_logged", fake_run_logged)

    code, manifest_path = run_cycle(options)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert code == 1
    imports = manifest["system_integration"]["imports"]
    assert imports[0]["platform"] == "51job"
    assert imports[0]["status"] == "failed"
    assert imports[1]["platform"] == "liepin"
    assert imports[1]["status"] == "blocked"
    assert imports[1]["reason"] == "platform_collection_failed"


def test_non_interactive_cycle_defers_liepin_without_failing_other_sources(tmp_path, monkeypatch):
    options = make_options(
        tmp_path,
        platforms=("51job", "liepin"),
        dry_run=False,
        system_import=False,
        collection_limit=1,
        scan_mode="target",
        non_interactive=True,
    )
    invoked = []

    def fake_run_logged(command, log_path, cwd, append=False):
        del cwd, append
        invoked.append(" ".join(command))
        output_dir = Path(command[command.index("--output-dir") + 1])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "jobs_2026_it.csv").write_text(
            "jobID,职位名称\n1,测试岗位\n", encoding="utf-8"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ok\n", encoding="utf-8")
        return 0, 0.01

    monkeypatch.setattr("job_crawler_runner.run_logged", fake_run_logged)
    monkeypatch.setattr("job_crawler_runner.liepin_login_ready", lambda _options: False)
    code, manifest_path = run_cycle(options)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert code == 0
    assert manifest["status"] == "success"
    assert manifest["platforms"][1]["status"] == "skipped_auth"
    assert manifest["platforms"][1]["skip_reason"] == "interactive_login_required"
    assert all("liepin_cdp_raw.py" not in command for command in invoked)


def test_full_and_quick_modes_use_shared_keyword_pool(tmp_path):
    full = make_options(tmp_path, scan_mode="full")
    quick = make_options(tmp_path, scan_mode="quick")
    assert len(scan_keywords(full)) == 73
    assert len(scan_keywords(quick)) == 12
    assert set(scan_keywords(quick)).issubset(scan_keywords(full))


def test_capped_csv_contains_only_newest_bounded_rows(tmp_path):
    source = tmp_path / "source.csv"
    source.write_text("id,name\n1,A\n2,B\n3,C\n4,D\n", encoding="utf-8")
    target = tmp_path / "capped.csv"
    assert write_capped_csv(source, target, 2) == 2
    rows = target.read_text(encoding="utf-8-sig").splitlines()
    assert rows == ["id,name", "3,C", "4,D"]


def test_logged_child_process_uses_utf8_output(tmp_path):
    script = tmp_path / "unicode_child.py"
    script.write_text("print('✅ non-breaking‑hyphen')\n", encoding="utf-8")
    log_path = tmp_path / "child.log"

    code, _duration = run_logged(
        [sys.executable, str(script)], log_path, tmp_path
    )

    assert code == 0
    assert log_path.read_text(encoding="utf-8") == "✅ non-breaking‑hyphen\n"


def test_acquire_lock_reclaims_stale_pid_file(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    lock = output / "crawler.lock"
    lock.write_text("999999999", encoding="ascii")

    acquired = acquire_lock(output)

    assert acquired == lock
    assert int(lock.read_text(encoding="ascii")) > 0
    lock.unlink()


def test_acquire_lock_rejects_live_process(tmp_path):
    first = acquire_lock(tmp_path / "output")
    try:
        with pytest.raises(RuntimeError, match="已有采集任务"):
            acquire_lock(tmp_path / "output")
    finally:
        first.unlink()
