"""Run one bounded new-role discovery job against the live Neo4j graph."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .neo4j_source import inspect_neo4j_source
from .workbench import EvolutionWorkbenchService


FEATURE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = FEATURE_DIR.parent


def _api_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{base_url.rstrip('/')}/{path.lstrip('/')}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - configured local API
            value = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"岗位演化 API 调用失败：{type(error).__name__}") from error
    if not isinstance(value, dict):
        raise RuntimeError("岗位演化 API 返回了无效结果")
    return value


def _date(value: str) -> str:
    datetime.strptime(value, "%Y-%m-%d")
    return value


def _default_window(config_path: Path) -> tuple[str, str]:
    source = inspect_neo4j_source(config_path)
    as_of = datetime.strptime(
        str(source["max_posted_at"])[:10],
        "%Y-%m-%d",
    ).date()
    month_index = as_of.year * 12 + as_of.month - 1 - 3
    cutoff = date(month_index // 12, month_index % 12 + 1, 1)
    return cutoff.isoformat(), as_of.isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="直接使用 Neo4j 全局图谱运行一次新岗位涌现 Demo"
    )
    parser.add_argument("--cutoff", type=_date)
    parser.add_argument("--as-of", type=_date)
    parser.add_argument(
        "--llm-mode",
        choices=("off", "auto", "required"),
        default="required",
    )
    parser.add_argument("--role-limit", type=int, default=50)
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=0,
        help="只读取最近 N 条 IT JD；0 表示全量，适合快速演示",
    )
    parser.add_argument(
        "--skill-limit",
        type=int,
        default=40,
        help="最多语义复核的旧岗位能力变化候选数；设为 0 可关闭",
    )
    parser.add_argument(
        "--skill-source",
        default="",
        help="能力变化比较限定的数据来源；留空表示使用窗口内全部来源",
    )
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument(
        "--neo4j-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "neo4j_connection.json",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=PROJECT_ROOT / "output" / "role_evolution_workbench_v2",
    )
    parser.add_argument(
        "--api-url",
        default=os.getenv("EVOLUTION_API_URL", "").strip(),
        help="可选：由常驻工作台 API 统一登记任务；生产自动化应配置本机地址",
    )
    args = parser.parse_args()

    config_path = args.neo4j_config.expanduser().resolve()
    cutoff, as_of = _default_window(config_path)
    if args.cutoff:
        cutoff = args.cutoff
    if args.as_of:
        as_of = args.as_of
    if cutoff >= as_of:
        parser.error("cutoff 必须早于 as-of")

    service = None
    try:
        payload = {
            "cutoff": cutoff,
            "as_of": as_of,
            "baseline_days": 0,
            "current_days": (
                datetime.strptime(as_of, "%Y-%m-%d")
                - datetime.strptime(cutoff, "%Y-%m-%d")
            ).days,
            "sample_limit": max(0, min(args.sample_limit, 20000)),
            "role_review_limit": max(0, min(args.role_limit, 50)),
            "skill_review_limit": max(0, min(args.skill_limit, 100)),
            "skill_change_source_family": args.skill_source.strip(),
            "llm_mode": args.llm_mode,
        }
        if args.api_url:
            task = _api_json(args.api_url, "/runs", method="POST", payload=payload)
        else:
            service = EvolutionWorkbenchService(config_path, args.data_root)
            task = service.create_run(payload)
        deadline = time.monotonic() + max(30, args.timeout_seconds)
        last_progress = -1
        while time.monotonic() < deadline:
            task = (
                _api_json(args.api_url, f"/runs/{task['task_id']}")
                if args.api_url
                else service.get_run(task["task_id"])
            )
            progress = int(task.get("progress") or 0)
            if progress != last_progress:
                print(
                    f"[{progress:3d}%] {task.get('message') or task.get('stage')}",
                    flush=True,
                )
                last_progress = progress
            if task["status"] in {
                "REVIEW_READY",
                "DEGRADED_REVIEW_READY",
                "FAILED",
                "INTERRUPTED",
            }:
                break
            time.sleep(1)
        if task["status"] not in {"REVIEW_READY", "DEGRADED_REVIEW_READY"}:
            print(
                json.dumps(task, ensure_ascii=False, indent=2),
                flush=True,
            )
            return 1
        result = (
            _api_json(args.api_url, f"/runs/{task['task_id']}/result")
            if args.api_url
            else service.get_result(task["task_id"])
        )
        candidates = result.get("new_role_candidates") or []
        skill_changes = result.get("role_skill_changes") or []
        summary = result.get("manifest", {}).get("summary") or task.get(
            "summary",
            {},
        )
        output = {
            "task_id": task["task_id"],
            "run_id": task.get("run_id"),
            "window": {"cutoff": cutoff, "as_of": as_of},
            "summary": summary,
            "top_candidates": [
                {
                    "candidate_id": candidate.get("candidate_id"),
                    "title": candidate.get("candidate_title"),
                    "rule_state": candidate.get("rule_state"),
                    "emergence_score": candidate.get("emergence_score"),
                    "growth_windows": candidate.get("growth_windows"),
                    "independent_sources": candidate.get(
                        "independent_source_count"
                    ),
                    "confirmation_state": candidate.get(
                        "confirmation_state"
                    ),
                }
                for candidate in candidates[:5]
            ],
            "top_ability_changes": [
                {
                    "role": candidate.get("role"),
                    "skill": candidate.get("skill"),
                    "rule_state": candidate.get("rule_state"),
                    "change_type": candidate.get("change_type"),
                    "coverage_delta": candidate.get("delta"),
                    "confirmation_state": candidate.get("confirmation_state"),
                }
                for candidate in skill_changes[:5]
            ],
            "warnings": task.get("warnings") or [],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2), flush=True)
        return 0
    finally:
        if service is not None:
            service.close(wait=True)


if __name__ == "__main__":
    raise SystemExit(main())
