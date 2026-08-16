"""逐条运行第一批AI岗位判断，输出可导入JSONL，并支持断点续跑。"""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path


PROJECT = Path(__file__).resolve().parent
WORKSPACE = PROJECT.parents[1]
RESULTS = WORKSPACE / "2026数据51job" / "岗位概念标准化结果"


def main() -> int:
    parser = argparse.ArgumentParser(description="运行AI岗位概念判断；API Key采用隐藏输入")
    parser.add_argument("--input", type=Path, default=RESULTS / "ai_batch_1_high_evidence.jsonl")
    parser.add_argument("--output", type=Path, default=RESULTS / "ai_decisions.jsonl")
    parser.add_argument("--model", default=os.getenv("ROLE_AI_MODEL", "gpt-4o-mini"))
    parser.add_argument("--base-url", default=os.getenv("ROLE_AI_BASE_URL", "https://api.openai.com/v1/chat/completions"))
    parser.add_argument("--limit", type=int, default=0, help="本次最多处理多少条；0表示全部")
    args = parser.parse_args()
    key = os.getenv("ROLE_AI_API_KEY", "").strip() or getpass.getpass("请输入AI API Key（输入不会显示）：")
    if not key:
        raise ValueError("未提供AI API Key")
    os.environ.update({"ROLE_AI_REVIEW_ENABLED": "1", "ROLE_AI_API_KEY": key,
                       "ROLE_AI_MODEL": args.model, "ROLE_AI_BASE_URL": args.base_url})
    from concept_standardization.ai_judge import AIRoleJudge
    judge = AIRoleJudge()
    completed: set[str] = set()
    if args.output.exists():
        with args.output.open("r", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    completed.add(str(json.loads(line).get("candidate_id") or ""))
    tasks = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()]
    pending = [x for x in tasks if str(x.get("candidate_id") or "") not in completed]
    if args.limit > 0:
        pending = pending[:args.limit]
    success = failed = 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("a", encoding="utf-8") as stream:
        for index, task in enumerate(pending, start=1):
            result = judge.judge(task, list(task.get("top_candidates") or []))
            if result.get("status") != "COMPLETED":
                failed += 1
                print(f"[{index}/{len(pending)}] 失败：{task.get('source_name')}｜{result.get('reason')}")
                continue
            analysis = dict(result["analysis"])
            analysis.update({"candidate_id": task["candidate_id"], "model_version": args.model})
            stream.write(json.dumps(analysis, ensure_ascii=False) + "\n"); stream.flush()
            success += 1
            print(f"[{index}/{len(pending)}] 完成：{task.get('source_name')} → {analysis.get('decision')}")
    print(json.dumps({"already_completed": len(completed), "success": success, "failed": failed,
                      "remaining": max(0, len(tasks) - len(completed) - success)}, ensure_ascii=False, indent=2))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
