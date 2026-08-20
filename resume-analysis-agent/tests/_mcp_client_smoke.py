"""Connect to mcp_server.py using the official mcp 2.0 client (as Codex would)."""

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
PYTHON = os.environ.get("RESUME_MCP_PYTHON", sys.executable)


async def main():
    params = StdioServerParameters(
        command=PYTHON,
        args=["mcp_server.py"],
        cwd=str(ROOT),
        env={k: v for k, v in os.environ.items()},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await asyncio.wait_for(session.initialize(), timeout=30)
            print("initialize OK:")
            print("  protocol:", init.protocol_version)
            print("  server:", init.server_info)
            tools = await asyncio.wait_for(session.list_tools(), timeout=30)
            names = sorted(t.name for t in tools.tools)
            print("tools/list OK:", names)
            resources = await asyncio.wait_for(session.list_resources(), timeout=30)
            rnames = sorted(r.uri for r in resources.resources)
            print("resources/list OK:", rnames)
            assert {
                "rank_resume",
                "prepare_enhance",
                "apply_enhance_review",
                "prepare_gap",
                "prepare_resume_edit",
                "validate_resume_edit",
                "prepare_resume_extract",
                "apply_resume_extract",
                "visualize_radar",
            } <= set(names)
            resume = (
                "张三，硕士，计算机科学与技术。熟练掌握 Python、PyTorch、深度学习、"
                "Transformer 架构、大语言模型微调（SFT/RLHF/LoRA），有分布式训练经验，"
                "参与过推荐系统项目，熟悉 C++，对 AI 技术充满热情，自驱力强。"
            )
            result = await asyncio.wait_for(
                session.call_tool("rank_resume", {"resume_text": resume, "topk": 3}),
                timeout=60,
            )
            content = result.content[0].text if result.content else ""
            import json

            data = json.loads(content)
            print("rank_resume OK:")
            for item in data.get("results", [])[:3]:
                print(
                    f"  {item['role_name']}  score={item['score']:.1%}  "
                    f"hits={item['hit_skills']}/{item['total_skills']}"
                )
            extract_result = await asyncio.wait_for(
                session.call_tool(
                    "apply_resume_extract",
                    {
                        "resume_text": resume,
                        "extract_json": json.dumps(
                            {
                                "dimensions": {
                                    "knowledge": ["Python", "深度学习"],
                                    "skill": ["PyTorch", "Transformer 微调", "分布式训练"],
                                    "qualifications": [],
                                    "preference": [],
                                    "motivation": ["对 AI 技术充满热情"],
                                    "trait": ["自驱力强"],
                                    "self_concept": [],
                                }
                            },
                            ensure_ascii=False,
                        ),
                    },
                ),
                timeout=60,
            )
            extract_data = json.loads(extract_result.content[0].text)
            print("apply_resume_extract OK: valid =", extract_data["validation"]["ok"])
            print("CLIENT SMOKE TEST PASSED")


if __name__ == "__main__":
    asyncio.run(main())

