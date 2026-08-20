"""MCP 服务入口 — 简历人岗匹配工具套件。

唯一平台边界：所有逻辑都在 src/tools/ 与 src/core/ 中，
本文件只负责注册工具与资源，不包含业务代码。

运行:
    python mcp_server.py          # stdio 传输（默认）
    python mcp_server.py --transport sse  # SSE 传输
"""

import json
import sys
from pathlib import Path

# 保证以源码方式运行时能 import src
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 主动加载项目根目录 .env（CLI 的 enhance/analyze/modify 需要 LLM_PROVIDER 对应供应商的
# API key，如 DEEPSEEK_API_KEY；
# MCP 模式不调用 LLM API，LLM 推理由调用方 Agent 自己的模型完成）
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

# mcp SDK 版本兼容：1.x 用 FastMCP，2.x 用 MCPServer（两者对下方用法接口一致）
try:
    from mcp.server.fastmcp import FastMCP, Image  # mcp 1.x
    _ServerCls = FastMCP
except ImportError:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer, Image
    _ServerCls = MCPServer

from src.core.dimensions import (
    DIMENSION_KEYS,
    DIM_LABELS,
    DIM_TO_CATEGORY,
    CATEGORY_TO_DIM,
)
from src.tools.rank import rank_resume as _rank_resume
from src.tools.enhance import prepare_enhance as _prepare_enhance
from src.tools.enhance import apply_enhance_review as _apply_enhance_review
from src.tools.analyze import prepare_gap as _prepare_gap
from src.tools.modify import prepare_resume_edit as _prepare_resume_edit
from src.tools.modify import validate_resume_edit as _validate_resume_edit
from src.tools.visualize import render_radar as _render_radar
from src.tools.resume_extract import prepare_resume_extract as _prepare_resume_extract
from src.tools.resume_extract import apply_resume_extract as _apply_resume_extract

mcp = _ServerCls("resume-analysis", instructions="简历岗位匹配分析（resume-analysis）：关键词命中粗排 → Agent 语义复核 → 差距分析 → 简历修改建议 → 简历画像提取（7 维）→ 雷达图。MCP 模式不调用外部 LLM API，语义复核/差距分析/修改建议/画像提取由调用方 Agent 用自己的大模型完成。")


# ==================== 静态资源 ====================

@mcp.resource("dimensions://seven")
def dimensions_resource() -> str:
    """七维技能分类定义（供 Agent 参考）。"""
    lines = [
        f"- {dim}: {DIM_LABELS[dim]}（NormalizedSkill.category: {DIM_TO_CATEGORY[dim]}）"
        for dim in DIMENSION_KEYS
    ]
    return (
        "七维画像定义（核心口径，严格对齐图谱 NormalizedSkill.category）：\n"
        + "\n".join(lines)
        + "\n\n注：大纲'五分类'（知识/技术/动机/特质/自我概念）仅为原始数据/汇报口径，"
        "对外材料可用 project_to_five_dim() 做 7→5 投影。"
    )


@mcp.resource("dimensions://category-map")
def category_map_resource() -> str:
    """Neo4j NormalizedSkill.category → 七维 key 映射。"""
    return json.dumps(CATEGORY_TO_DIM, ensure_ascii=False, indent=2)


# ==================== 工具 ====================

@mcp.tool()
def rank_resume(resume_text: str, topk: int = 10, use_idf: bool = False) -> dict:
    """对简历原文做关键词命中粗排，返回 Top-N Role 及七维覆盖率。

    Args:
        resume_text: 简历 Markdown 原文（PDF/DOCX 需先用 markitdown 转换）。
        topk: 返回前 N 名（默认 10）。
        use_idf: 是否启用跨岗位 IDF 重加权（默认 False，消融对比用）。

    Returns:
        {"topk", "count", "results": [{role_name, family_name, domain_name,
                                       score, hit_skills, total_skills, dimensions}]}
    """
    return _rank_resume(resume_text, topk=topk, use_idf=use_idf)


@mcp.tool()
def prepare_enhance(rank_json: str, resume_text: str, topk: int = 20) -> dict:
    """为语义复核准备提示包：返回复核提示词 + 精简排名数据 + 输出 schema。

    MCP 模式不调用 LLM API：Agent 拿到提示包后，用自己的大模型完成复核，
    再调用 apply_enhance_review(rank_json, review_json) 合并规范化结果。

    Args:
        rank_json: rank_resume 返回结果的 JSON 字符串。
        resume_text: 简历 Markdown 原文。
        topk: 复核前 N 名（默认 20）。

    Returns:
        {"mode", "prompt", "rank_data", "resume_text", "output_schema", "next_step"}
    """
    rank_result = json.loads(rank_json)
    return _prepare_enhance(rank_result, resume_text, topk=topk)


@mcp.tool()
def apply_enhance_review(rank_json: str, review_json: str) -> dict:
    """把 Agent 复核后的 JSON 合并回粗排结果，重算覆盖率与得分（纯逻辑）。

    Args:
        rank_json: rank_resume 返回结果的完整 JSON 字符串。
        review_json: Agent 按 prepare_enhance 的输出 schema 生成的复核 JSON。

    Returns:
        {"topk", "results": [{role_name, score, hit_skills, total_skills,
                              review_note, dimensions}]}
    """
    rank_result = json.loads(rank_json)
    review = json.loads(review_json)
    return _apply_enhance_review(rank_result, review)


@mcp.tool()
def visualize_radar(role_json: str, role_name: str = "") -> Image:
    """渲染单个 Role 的七维雷达图并返回 PNG 图片。

    Args:
        role_json: rank_resume 结果中单个 role 的 JSON 字符串。
        role_name: 显示用岗位名（缺省取 role_name 字段）。

    Returns:
        PNG 图片，可直接在对话中渲染。
    """
    role = json.loads(role_json)
    path = _render_radar(role, role_name)
    return Image(path=path)


@mcp.tool()
def prepare_gap(role_json: str, resume_text: str) -> dict:
    """为差距分析准备提示包：返回分析提示词 + 岗位命中明细 + 简历原文。

    Agent 用自己的模型生成 Markdown 报告（匹配结论 / 各维分析 / 总体建议 / 学习路径）。

    Args:
        role_json: rank_resume 或复核结果中单个 role 的 JSON 字符串。
        resume_text: 简历 Markdown 原文。

    Returns:
        {"mode", "prompt", "role_name", "dimension_details", "resume_text", "output_format"}
    """
    role = json.loads(role_json)
    return _prepare_gap(role, resume_text)


@mcp.tool()
def prepare_resume_edit(role_json: str, resume_text: str) -> dict:
    """为简历修改准备提示包：目标岗位技能命中明细 + 修改规则 + 输出 schema。

    Agent 用自己的模型产出针对性修改建议（不重写全文，遵守真实性红线）。

    Args:
        role_json: rank_resume 或复核结果中单个 role 的 JSON 字符串。
        resume_text: 简历 Markdown 原文。

    Returns:
        {"mode", "prompt", "role_name", "skill_lines", "resume_text",
         "ai_phrase_blacklist", "output_schema"}
    """
    role = json.loads(role_json)
    return _prepare_resume_edit(role, resume_text)


@mcp.tool()
def validate_resume_edit(role_json: str, resume_text: str, edit_json: str) -> dict:
    """对 Agent 生成的简历修改建议做防造假校验（纯逻辑，不调用 LLM）。

    校验：技能是否在岗位技能清单、状态一致性、量化指标是否有简历依据、
    AI 味词汇。返回 {"valid", "summary", "violations", "stats", "checklist"}。

    Args:
        role_json: 单个 role 的 JSON 字符串（含 dimensions hit/miss）。
        resume_text: 简历 Markdown 原文。
        edit_json: prepare_resume_edit 输出 schema 对应的建议 JSON 字符串。

    Returns:
        防造假校验报告。
    """
    role = json.loads(role_json)
    edit = json.loads(edit_json)
    return _validate_resume_edit(role, resume_text, edit)


@mcp.tool()
def prepare_resume_extract(resume_text: str, position: str = "") -> dict:
    """为简历画像提取准备提示包：返回提取提示词 + 输出 schema。

    MCP 模式不调用 LLM API；Agent 拿到提示包后用自己的模型输出 7 维画像 JSON，
    再调用 apply_resume_extract(resume_text, extract_json) 规范化并校验。

    Args:
        resume_text: 简历 Markdown 原文。
        position: 目标岗位（可选，缺省按简历求职意向）。

    Returns:
        {"mode", "prompt", "resume_text", "position", "output_schema", "next_step"}
    """
    return _prepare_resume_extract(resume_text, position or None)


@mcp.tool()
def apply_resume_extract(resume_text: str, extract_json: str) -> dict:
    """把 Agent 产出的简历画像 JSON 规范化为 7 维画像并做防幻觉校验（纯逻辑，不调用 LLM）。

    超长条目（>30 字）自动截断并记录到 truncations；校验条目是否来自原文、
    去重、维度白名单。返回 {"position", "dimensions", "truncations", "stats", "validation"}。

    Args:
        resume_text: 简历 Markdown 原文。
        extract_json: Agent 按 prepare_resume_extract 的 schema 产出的提取 JSON 字符串。

    Returns:
        规范化后的 7 维画像 + 校验报告。
    """
    extract = json.loads(extract_json)
    return _apply_resume_extract(resume_text, extract)


# ==================== 入口 ====================

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="简历人岗匹配 MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="传输方式（默认 stdio，供 Claude Desktop/Codex/Cursor 等本地接入）",
    )
    args = parser.parse_args()
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()

