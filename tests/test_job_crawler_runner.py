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
    run_cycle,
)


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
