"""render_radar 工具 — 七维雷达图 PNG 渲染。"""

import os
import tempfile
from typing import Any, Dict, Optional

from ..core.dimensions import DIMENSION_KEYS, DIM_LABELS


def _plot_modules():
    """延迟加载绘图库，避免无 GUI 的 MCP stdio 握手被字体扫描阻塞。"""
    import matplotlib

    matplotlib.use("Agg")
    matplotlib.rcParams["font.sans-serif"] = [
        "SimHei",
        "Microsoft YaHei",
        "DejaVu Sans",
    ]
    matplotlib.rcParams["axes.unicode_minus"] = False

    import matplotlib.pyplot as plt
    import numpy as np

    return plt, np


def _coverage_values(role: Dict[str, Any]) -> Dict[str, float]:
    """提取七维覆盖率（缺省维度按 0.0）。"""
    dims = role.get("dimensions") or {}
    values = {}
    for dim in DIMENSION_KEYS:
        detail = dims.get(dim) or {}
        values[dim] = float(detail.get("coverage", 0.0) or 0.0)
    return values


def render_radar(
    role: Dict[str, Any],
    role_name: str = "",
    output_path: Optional[str] = None,
) -> str:
    """渲染单个 Role 的七维雷达图并保存 PNG。

    Args:
        role: 单个 Role JSON（含 dimensions 覆盖率），来自 rank_resume() 的 results[i]。
        role_name: 显示用岗位名；缺省取 role["role_name"]。
        output_path: PNG 输出路径；缺省写入系统临时目录。

    Returns:
        PNG 文件的绝对路径。
    """
    if not role:
        raise ValueError("role 不能为空。")
    name = role_name or role.get("role_name", "目标岗位")
    plt, np = _plot_modules()

    values = _coverage_values(role)
    labels = [DIM_LABELS[d] for d in DIMENSION_KEYS]
    nums = [values[d] for d in DIMENSION_KEYS]

    angles = np.linspace(0, 2 * np.pi, len(labels), endpoint=False).tolist()
    nums_closed = nums + nums[:1]
    angles_closed = angles + angles[:1]

    fig, ax = plt.subplots(figsize=(6, 6), subplot_kw={"polar": True})
    ax.set_theta_offset(np.pi / 2)
    ax.set_theta_direction(-1)
    ax.set_ylim(0, 1)
    ax.set_yticks([0.2, 0.4, 0.6, 0.8, 1.0])
    ax.set_yticklabels(["0.2", "0.4", "0.6", "0.8", "1.0"], fontsize=8)
    ax.set_xticks(angles)
    ax.set_xticklabels(labels, fontsize=11)
    ax.plot(angles_closed, nums_closed, color="#1f77b4", linewidth=2)
    ax.fill(angles_closed, nums_closed, color="#1f77b4", alpha=0.25)
    ax.set_title(name, pad=20, fontsize=14, fontweight="bold")
    fig.tight_layout()

    path = output_path or os.path.join(
        tempfile.gettempdir(), f"radar_{name}.png"
    )
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return os.path.abspath(path)
