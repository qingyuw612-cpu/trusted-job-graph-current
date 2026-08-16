from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


def _md_cell(value: Any) -> str:
    """Render untrusted values safely inside a Markdown table cell."""
    if value is None or value == "":
        text = "—"
    elif isinstance(value, bool):
        text = "是" if value else "否"
    elif isinstance(value, (dict, list, tuple, set)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    return (
        text.replace("\\", "&#92;")
        .replace("|", "&#124;")
        .replace("\r\n", "<br>")
        .replace("\n", "<br>")
        .replace("\r", "<br>")
        .replace("<script", "&lt;script")
        .replace("</script", "&lt;/script")
    )


def _table(headers: list[str], rows: Iterable[Iterable[Any]]) -> list[str]:
    rendered = [[_md_cell(value) for value in row] for row in rows]
    output = [
        "| " + " | ".join(_md_cell(header) for header in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    output.extend("| " + " | ".join(row) + " |" for row in rendered)
    return output


def _percentage(value: Any, *, signed: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:+.1%}" if signed else f"{number:.1%}"


def _decimal(value: Any, digits: int = 4) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _integer(value: Any) -> str:
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "—"


def _join(values: Any) -> str:
    if not values:
        return "—"
    if isinstance(values, (list, tuple, set)):
        return "；".join(str(value) for value in values)
    return str(values)


def _semantic_fields(candidate: dict[str, Any]) -> tuple[str, str, str, str]:
    semantic = candidate.get("semantic_review") or {}
    analysis = semantic.get("analysis") or {}
    confidence = analysis.get("confidence")
    return (
        str(semantic.get("status") or "未调用"),
        str(analysis.get("semantic_class") or "—"),
        _decimal(confidence, 2) if confidence is not None else "—",
        str(analysis.get("recommended_action") or semantic.get("error") or "—"),
    )


def _window_value(
    summary: dict[str, Any],
    quality: dict[str, Any],
    name: str,
) -> Any:
    if summary.get(name) not in (None, ""):
        return summary[name]
    windows = quality.get("windows") or {}
    return windows.get(name)


def write_markdown_report(
    path: Path,
    summary: dict[str, Any],
    quality: dict[str, Any],
    role_candidates: list[dict[str, Any]],
    skill_changes: list[dict[str, Any]],
    review_queue: list[dict[str, Any]],
    llm_usage: dict[str, Any],
) -> None:
    """Write a deterministic Chinese conclusion report from supplied run facts."""
    max_role_rows = 30
    max_skill_rows = 30
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    role_states = Counter(
        str(candidate.get("rule_state") or "UNKNOWN") for candidate in role_candidates
    )
    skill_states = Counter(
        str(candidate.get("rule_state") or "UNKNOWN") for candidate in skill_changes
    )
    human_states = Counter(
        str(task.get("status") or "UNKNOWN") for task in review_queue
    )

    lines: list[str] = [
        "# 岗位演化检测结论报告",
        "",
        "> 本报告严格区分算法候选、MaaS Qwen 语义复核意见和人工最终决策。"
        "候选及模型意见均不能直接视为已确认事实，图谱写回仍需人工批准。",
        "",
        "## 一、运行概览",
        "",
    ]
    lines.extend(
        _table(
            ["指标", "结果"],
            [
                ["基线起点", _window_value(summary, quality, "baseline_start")],
                ["新旧数据分界点", _window_value(summary, quality, "cutoff")],
                ["数据截止点", _window_value(summary, quality, "as_of")],
                ["可用招聘信息", summary.get("jds", quality.get("usable_jds"))],
                ["分界点前招聘信息", (
                    summary.get("historical_jds", quality.get("historical_jds"))
                    or 0
                ) + (
                    summary.get("baseline_jds", quality.get("baseline_jds"))
                    or 0
                )],
                ["近期招聘信息", summary.get("current_jds", quality.get("current_jds"))],
                ["企业数", summary.get("companies")],
                ["岗位数", summary.get("roles")],
                ["已验证能力证据边", summary.get("verified_skill_edges")],
                ["已映射标准能力边", summary.get("normalized_skill_edges")],
                ["历史未出现且近期达标的名称簇", len(role_candidates)],
                ["人工复核任务数", len(review_queue)],
                ["运行模式", "只读演练（dry-run）" if summary.get("dry_run") else "非演练"],
            ],
        )
    )

    lines.extend(
        [
            "",
            "## 二、数据质量",
            "",
        ]
    )
    lines.extend(
        _table(
            ["指标", "结果"],
            [
                ["质量状态", quality.get("status")],
                ["数据库 JD", quality.get("database_jds")],
                ["日期有效 JD", quality.get("valid_date_jds")],
                ["日期无效 JD", quality.get("invalid_date_jds")],
                ["日期完整率", _percentage(quality.get("date_completeness"))],
                ["基线企业数", quality.get("baseline_companies")],
                ["新窗口企业数", quality.get("current_companies")],
                ["来源分布 JSD", _decimal(quality.get("source_js_divergence"))],
                ["行业分布 JSD", _decimal(quality.get("industry_js_divergence"))],
                [
                    "能力标准化覆盖率",
                    _percentage(
                        (quality.get("skill_normalization") or {}).get(
                            "normalization_coverage"
                        )
                    ),
                ],
            ],
        )
    )
    warnings = quality.get("warnings") or []
    lines.extend(["", "质量警告："])
    if warnings:
        lines.extend(f"- `{_md_cell(warning)}`" for warning in warnings)
    else:
        lines.append("- 无")

    lines.extend(
        [
            "",
            "## 三、算法候选层",
            "",
            "本层仅呈现统计规则筛出的候选，不代表新岗位或能力变化已经成立。",
            "",
            "### 3.1 新岗位发现候选",
            "",
        ]
    )
    lines.extend(
        _table(
            ["状态", "数量"],
            sorted(role_states.items(), key=lambda item: item[0]),
        )
        if role_states
        else ["本次没有新岗位候选。"]
    )
    if role_candidates:
        role_rows: list[list[Any]] = []
        for candidate in role_candidates[:max_role_rows]:
            nearest = (candidate.get("nearest_existing_roles") or [{}])[0]
            semantic_status, semantic_class, confidence, _ = _semantic_fields(candidate)
            role_rows.append(
                [
                    candidate.get("rule_state"),
                    candidate.get("candidate_title"),
                    candidate.get("historical_jd_count"),
                    candidate.get("current_jd_count"),
                    candidate.get("current_company_count"),
                    candidate.get("current_template_count"),
                    _decimal(candidate.get("emergence_score"), 1),
                    nearest.get("role"),
                    _decimal(nearest.get("weighted_skill_jaccard")),
                    _join(candidate.get("rule_reasons")),
                    semantic_status,
                    semantic_class,
                    confidence,
                ]
            )
        lines.extend(["", "候选明细：", ""])
        lines.extend(
            _table(
                [
                    "规则状态",
                    "候选岗位名",
                    "历史招聘信息",
                    "近期招聘信息",
                    "近期企业",
                    "近期模板",
                    "涌现分",
                    "最近既有岗位",
                    "能力 Jaccard",
                    "规则依据",
                    "语义复核状态",
                    "语义复核分类",
                    "置信度",
                ],
                role_rows,
            )
        )
        if len(role_candidates) > max_role_rows:
            lines.append(
                f"\n仅展示前 {max_role_rows} 条；全部 {len(role_candidates)} 条见 "
                "`new_role_candidates.json`。"
            )

    lines.extend(
        [
            "",
            "### 3.2 既有岗位能力变化候选",
            "",
        ]
    )
    lines.extend(
        _table(
            ["状态", "数量"],
            sorted(skill_states.items(), key=lambda item: item[0]),
        )
        if skill_states
        else ["本次没有能力变化候选。"]
    )
    if skill_changes:
        skill_rows: list[list[Any]] = []
        for candidate in skill_changes[:max_skill_rows]:
            semantic_status, semantic_class, confidence, _ = _semantic_fields(candidate)
            baseline = (
                f"{_integer(candidate.get('baseline_company_count'))}/"
                f"{_integer(candidate.get('baseline_role_companies'))} "
                f"({_percentage(candidate.get('baseline_coverage'))})"
            )
            current = (
                f"{_integer(candidate.get('current_company_count'))}/"
                f"{_integer(candidate.get('current_role_companies'))} "
                f"({_percentage(candidate.get('current_coverage'))})"
            )
            skill_rows.append(
                [
                    candidate.get("rule_state"),
                    candidate.get("change_type"),
                    candidate.get("role"),
                    candidate.get("skill"),
                    candidate.get("normalization_status"),
                    baseline,
                    current,
                    _percentage(candidate.get("delta"), signed=True),
                    _decimal(candidate.get("q_value")),
                    _join(candidate.get("rule_reasons")),
                    semantic_status,
                    semantic_class,
                    confidence,
                ]
            )
        lines.extend(["", "候选明细：", ""])
        lines.extend(
            _table(
                [
                    "规则状态",
                    "变化类型",
                    "岗位",
                    "能力",
                    "能力映射状态",
                    "基线企业覆盖",
                    "新窗口企业覆盖",
                    "覆盖变化",
                    "q 值",
                    "规则依据",
                    "语义复核状态",
                    "语义复核分类",
                    "置信度",
                ],
                skill_rows,
            )
        )
        if len(skill_changes) > max_skill_rows:
            lines.append(
                f"\n仅展示前 {max_skill_rows} 条；全部 {len(skill_changes)} 条见 "
                "`role_skill_changes.json`。"
            )

    reviewed_candidates = [
        ("新岗位", candidate.get("candidate_title"), candidate)
        for candidate in role_candidates
        if candidate.get("semantic_review")
    ]
    reviewed_candidates.extend(
        (
            "能力变化",
            f"{candidate.get('role', '')} / {candidate.get('skill', '')}",
            candidate,
        )
        for candidate in skill_changes
        if candidate.get("semantic_review")
    )
    lines.extend(
        [
            "",
            "## 四、低成本语义复核层（规则护栏 + MaaS Qwen）",
            "",
        ]
    )
    lines.extend(
        _table(
            ["指标", "结果"],
            [
                ["是否启用", llm_usage.get("enabled")],
                ["未启用原因", llm_usage.get("disabled_reason")],
                ["提供方", llm_usage.get("provider")],
                ["模型", llm_usage.get("model")],
                ["提示词版本", llm_usage.get("prompt_version")],
                ["实际请求数", llm_usage.get("requests")],
                ["缓存命中数", llm_usage.get("cache_hits")],
                ["失败数", llm_usage.get("failures")],
                ["输入 token", llm_usage.get("prompt_tokens")],
                ["输出 token", llm_usage.get("completion_tokens")],
                ["总 token", llm_usage.get("total_tokens")],
            ],
        )
    )
    if reviewed_candidates:
        semantic_rows = []
        for kind, title, candidate in reviewed_candidates:
            status, semantic_class, confidence, recommendation = _semantic_fields(candidate)
            semantic = candidate.get("semantic_review") or {}
            semantic_rows.append(
                [
                    kind,
                    title,
                    semantic.get("source") or "—",
                    status,
                    semantic_class,
                    confidence,
                    recommendation,
                ]
            )
        lines.extend(["", "语义复核明细：", ""])
        lines.extend(
            _table(
                [
                    "候选类型",
                    "候选",
                    "复核来源",
                    "复核状态",
                    "语义分类",
                    "置信度",
                    "建议或错误",
                ],
                semantic_rows,
            )
        )
    else:
        lines.extend(["", "本次参数中没有规则护栏或 MaaS Qwen 语义复核结果。"])

    lines.extend(
        [
            "",
            "## 五、人工待审层",
            "",
        ]
    )
    lines.extend(
        _table(
            ["任务状态", "数量"],
            sorted(human_states.items(), key=lambda item: item[0]),
        )
        if human_states
        else ["本次没有人工待审任务。"]
    )
    if review_queue:
        task_rows = []
        for task in review_queue:
            semantic = task.get("semantic_review") or {}
            analysis = semantic.get("analysis") or {}
            task_rows.append(
                [
                    task.get("task_id"),
                    task.get("task_type"),
                    task.get("title"),
                    _join(task.get("rule_reasons")),
                    semantic.get("source") or "—",
                    semantic.get("status") or "未调用",
                    analysis.get("semantic_class"),
                    task.get("status"),
                    task.get("human_decision"),
                    task.get("human_comment"),
                ]
            )
        lines.extend(["", "待审任务：", ""])
        lines.extend(
            _table(
                [
                    "任务 ID",
                    "类型",
                    "候选",
                    "算法依据",
                    "复核来源",
                    "语义复核状态",
                    "语义复核分类",
                    "人工状态",
                    "人工决定",
                    "人工备注",
                ],
                task_rows,
            )
        )

    lines.extend(
        [
            "",
            "## 六、结论边界",
            "",
            "- `REVIEW` 表示进入复核队列，不表示已确认。",
            "- `WATCH` 表示已通过基础规则，但因本轮复核名额有限未进入 Top 候选。",
            "- 基础规则为：岗位名称聚类后，分界点前招聘信息为 0，近期招聘量、企业数和模板数达到配置门槛。",
            "- Qwen 只复核规则筛出的少量 Top 候选，用于判断新岗位、岗位细分、别名或噪声，不能替代统计规则和人工批准。",
            "- 能力在当前窗口缺失仅作为观察信号，不据此自动删除图谱能力项。",
            "- 本报告不改写正式 `Role`；候选快照和人工决定保存在独立的 Neo4j 版本审核子图中。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
